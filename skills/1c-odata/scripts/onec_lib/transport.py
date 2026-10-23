"""Bounded OData HTTP requests. Only GET may be retried."""
import base64
import datetime as dt
import email.utils
import json
import math
from http.client import HTTPException
import os
import posixpath
import time
import urllib.error
import urllib.parse as url
import urllib.request
from .common import Error, MAX_BYTES, WireNumber, wire_json, require


class Config:
    def __init__(self):
        self.base = os.environ.get("ONEC_ODATA_URL", "").rstrip("/") + "/"
        self.username = os.environ.get("ONEC_ODATA_USERNAME", "")
        self.password = os.environ.get("ONEC_ODATA_PASSWORD", "")
        parts = url.urlsplit(self.base)
        allow_http = os.environ.get("ONEC_ODATA_ALLOW_HTTP") == "1"
        require(parts.scheme == "https" or (parts.scheme == "http" and allow_http),
                "Нужен HTTPS URL; HTTP тестовой базы требует ONEC_ODATA_ALLOW_HTTP=1.", "configuration_error")
        require(bool(parts.netloc) and not parts.username and not parts.password and not parts.query and not parts.fragment,
                "Некорректный корневой OData URL.", "configuration_error")
        require(bool(self.username.strip()) and bool(self.password),
                "Задайте ONEC_ODATA_USERNAME и ONEC_ODATA_PASSWORD.", "configuration_error")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, config, sleep=time.sleep, allow_mutations=False):
        self.base, self.sleep = config.base, sleep
        self.allow_mutations = allow_mutations
        token = base64.b64encode((config.username + ":" + config.password).encode()).decode()
        self.headers = {"Authorization": "Basic " + token, "Accept": "application/json",
                        "DataServiceVersion": "3.0", "MaxDataServiceVersion": "3.0"}
        self.opener = urllib.request.build_opener(NoRedirect())

    def safe_url(self, address):
        target = url.urljoin(self.base, address)
        base, parts = url.urlsplit(self.base), url.urlsplit(target)
        decoded = url.unquote(parts.path)
        root = posixpath.normpath(url.unquote(base.path)).rstrip("/") + "/"
        path = posixpath.normpath(decoded)
        require(parts.scheme == base.scheme and parts.netloc == base.netloc and not parts.fragment
                and not parts.username and not parts.password and "\\" not in decoded
                and (path + "/").startswith(root), "Ссылка выходит за пределы OData-публикации.", "unsafe_url")
        return target

    def request(self, method, address, payload=None, metadata=False, headers=None, with_headers=False):
        require(method in ({"GET", "POST", "PATCH", "DELETE"} if self.allow_mutations else {"GET"}),
                "HTTP-метод запрещён.", "write_forbidden")
        target = self.safe_url(address)
        body = wire_json(payload).encode() if payload is not None else None
        require(body is None or len(body) <= MAX_BYTES, "Запрос превышает 16 МиБ.")
        extra_headers = headers or {}
        require(all(name == "If-Match" for name in extra_headers), "Заголовок не разрешён.")
        require(not extra_headers or method in {"PATCH", "DELETE"}, "If-Match разрешён только для PATCH/DELETE.")
        require(all(isinstance(value, str) and 0 < len(value) <= 4096
                    and all(32 <= ord(char) <= 126 for char in value) for value in extra_headers.values()),
                "Некорректная версия объекта.")
        headers = {**self.headers, "Content-Type": "application/json; charset=utf-8", **extra_headers}
        if metadata:
            headers["Accept"] = "application/xml"
        for attempt in range(4):
            status, response_headers, raw = None, {}, b""
            try:
                req = urllib.request.Request(target, data=body, headers=headers, method=method)
                try:
                    response = self.opener.open(req, timeout=30)
                except urllib.error.HTTPError as exc:
                    status, response_headers = exc.code, dict(exc.headers)
                    exc.close()
                else:
                    with response:
                        status, response_headers = response.status, dict(response.headers)
                        raw = response.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise Error("write_outcome_unknown" if method != "GET" else "response_too_large", "Ответ превышает 16 МиБ.")
            except (OSError, EOFError, HTTPException):
                status = None
            if status is not None and 200 <= status < 300:
                if metadata:
                    return (raw, response_headers) if with_headers else raw
                try:
                    result = json.loads(raw, parse_float=WireNumber) if raw else None
                    return (result, response_headers) if with_headers else result
                except (ValueError, UnicodeError):
                    raise Error("write_outcome_unknown" if method != "GET" else "invalid_response", "Некорректный JSON ответа.") from None
            transient = status is None or status in {429, 502, 503, 504}
            known_error = {401: "authentication_error", 403: "access_denied", 404: "object_not_found",
                           405: "operation_not_supported", 412: "concurrency_conflict"}.get(status)
            if known_error:
                raise Error(known_error, "OData-запрос отклонён сервером.", http_status=status)
            if method != "GET":
                code = "http_error" if status in {400, 401, 403, 404, 405, 409, 413, 415, 422} else "write_outcome_unknown"
                raise Error(code, "Запись не подтверждена; автоматического повтора нет.", http_status=status)
            if not transient or attempt == 3:
                raise Error("temporary_error" if transient else "http_error", "OData-запрос не выполнен.", http_status=status)
            wait = 2 ** attempt
            retry = next((v for k, v in response_headers.items() if k.lower() == "retry-after"), None)
            if retry:
                try:
                    seconds = float(retry)
                except ValueError:
                    try:
                        stamp = email.utils.parsedate_to_datetime(retry)
                        seconds = (stamp - dt.datetime.now(dt.timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        seconds = 0
                if not math.isfinite(seconds):
                    seconds = 0
                if seconds > 60:
                    raise Error("retry_later", "Сервер запросил отложенный повтор.", retry_after_seconds=seconds)
                wait = max(wait, seconds)
            self.sleep(wait)
