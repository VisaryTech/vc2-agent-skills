"""Bounded OData HTTP requests. POST is never retried here."""
import base64
import datetime as dt
import email.utils
import json
import math
from http.client import HTTPException
import os
from pathlib import Path
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
        require(self.username == "ai_accountant" and bool(self.password),
                "Задайте ONEC_ODATA_USERNAME=ai_accountant и ONEC_ODATA_PASSWORD.", "configuration_error")
        self.state_dir = Path(os.environ.get("ONEC_ODATA_STATE_DIR", "~/.local/state/onec-odata")).expanduser().resolve()
        repo = Path(__file__).resolve().parents[4]
        require(not self.state_dir.is_relative_to(repo), "Каталог состояния должен находиться вне репозитория.", "configuration_error")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, config, log=lambda **_: None, sleep=time.sleep):
        self.base, self.log, self.sleep = config.base, log, sleep
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

    def request(self, method, address, payload=None, metadata=False):
        require(method in {"GET", "POST"}, "HTTP-метод запрещён.", "write_forbidden")
        target = self.safe_url(address)
        body = wire_json(payload).encode() if payload is not None else None
        require(body is None or len(body) <= MAX_BYTES, "Запрос превышает 16 МиБ.")
        headers = {**self.headers, "Content-Type": "application/json; charset=utf-8"}
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
                        raise Error("write_outcome_unknown" if method == "POST" else "response_too_large", "Ответ превышает 16 МиБ.")
            except (OSError, EOFError, HTTPException):
                status = None
            self.log(event="http", method=method, status=status, attempt=attempt + 1)
            if status is not None and 200 <= status < 300:
                if metadata:
                    return raw
                try:
                    return json.loads(raw, parse_float=WireNumber) if raw else None
                except (ValueError, UnicodeError):
                    raise Error("write_outcome_unknown" if method == "POST" else "invalid_response", "Некорректный JSON ответа.") from None
            transient = status is None or status in {429, 502, 503, 504}
            if method == "POST":
                code = "http_error" if status in {400, 401, 403, 404, 405, 409, 413, 415, 422} else "write_outcome_unknown"
                raise Error(code, "Создание не подтверждено; автоматического повтора POST нет.", http_status=status)
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
