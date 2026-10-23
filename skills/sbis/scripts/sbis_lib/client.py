"""Authentication, JSON-RPC and document reading operations."""

import base64
import copy
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
import urllib.error

from .auth import ENV_KEYS, TokenCache, load_credentials, validate_credentials
from .common import DEFAULT_MAX_PAGES, HOSTS, MAX_FILE, MAX_REQUEST, ClientError, format_size, objects, require
from .transport import HttpTransport, file_url


class Client:
    def __init__(self, profile="production", transport=None, env=None, cache_root=None, env_file=None):
        self.profile = profile
        self.base = HOSTS[profile]
        self.transport = transport or HttpTransport()
        self.env = load_credentials(env, env_file)
        self.token = None
        self.secrets = [self.env.get(k, "") for k in ENV_KEYS]
        self.cache = TokenCache(profile, self.env, cache_root)
        self.counter = 0
        self.draft_snapshot = None

    def rpc(self, method, params, write=False):
        self.authenticate()
        self.counter += 1
        payload = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        headers["X-SBISAccessToken"] = self.token
        try:
            status, raw = self.transport.post(
                self.base + "/service/?srv=1", payload, headers)
            if status != 200:
                if write and status >= 500:
                    self.unknown_write()
                self.http_error(status)
            require(len(raw) <= MAX_REQUEST, "Слишком большой ответ сервера.", "invalid_response")
            data = json.loads(raw)
            require(isinstance(data, dict) and data.get("jsonrpc") == "2.0"
                    and data.get("id") == self.counter, "Некорректный ответ JSON-RPC.", "invalid_response")
            if "error" in data:
                error = data["error"]
                # Do not expose arbitrary server messages, traces, credentials or request echoes.
                code = error.get("code") if isinstance(error, dict) else None
                suffix = f" (код {code})" if isinstance(code, int) else ""
                raise ClientError("rpc_error", "СБИС вернул ошибку JSON-RPC" + suffix + ".")
            require("result" in data, "В ответе нет result.", "invalid_response")
            return data["result"]
        except urllib.error.HTTPError as exc:
            exc.close()
            if write and exc.code >= 500:
                self.unknown_write()
            self.http_error(exc.code)
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError, http.client.HTTPException):
            if write:
                self.unknown_write()
            raise ClientError("network_error", "Сеть недоступна или превышено время ожидания. Запрос не повторялся.") from None
        except (ValueError, UnicodeError):
            if write:
                self.unknown_write()
            raise ClientError("invalid_response", "Сервер вернул некорректный JSON.") from None
        except ClientError as exc:
            if write and exc.code == "invalid_response":
                self.unknown_write()
            raise

    @staticmethod
    def unknown_write():
        raise ClientError("write_outcome_unknown",
                          "Результат записи неизвестен. Запрос не повторялся. Проверьте документ в СБИС перед новой записью.")

    def http_error(self, status):
        if status == 401:
            self.token = None
            try:
                self.cache.clear()
            except OSError:
                print("Не удалось удалить приватный кэш токена.", file=sys.stderr)
            raise ClientError("token_expired", "Токен недействителен. Повторите безопасное чтение для нового входа; операция не повторялась.")
        if status == 403:
            raise ClientError("access_denied", "Нет доступа. Проверьте права и тариф; для файла обновите ссылку чтением документа.")
        raise ClientError("http_error", f"HTTP {status}. Запрос не повторялся.")

    def authenticate(self):
        if self.token:
            return
        validate_credentials(self.env)
        self.token = self.cache.load()
        if not self.token:
            payload = {key.removeprefix("SBIS_").lower(): self.env[key] for key in ENV_KEYS}
            try:
                status, raw = self.transport.post(self.base + "/oauth/service/", payload, {
                    "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"})
                if status != 200:
                    self.http_error(status)
                require(len(raw) <= MAX_REQUEST, "Слишком большой ответ сервера.", "invalid_response")
                data = json.loads(raw)
                require(isinstance(data, dict) and isinstance(data.get("token"), str)
                        and data["token"].strip(), "Не получен токен доступа.", "invalid_response")
                self.token = data["token"]
            except urllib.error.HTTPError as exc:
                exc.close()
                self.http_error(exc.code)
            except (TimeoutError, socket.timeout, urllib.error.URLError, OSError, http.client.HTTPException):
                raise ClientError("network_error", "Не удалось получить токен. Запрос не повторялся.") from None
            except (ValueError, UnicodeError):
                raise ClientError("invalid_response", "Сервер вернул некорректный JSON.") from None
            try:
                self.cache.save(self.token)
            except (OSError, ClientError):
                print("Токен получен, но сохранить приватный кэш не удалось.", file=sys.stderr)
        self.secrets.append(self.token)

    def read(self, identifier, *, include_extension=False):
        params = {"Идентификатор": identifier}
        if include_extension:
            params["ДопПоля"] = "Расширение"
        result = self.rpc("СБИС.ПрочитатьДокумент", {"Документ": params})
        require(isinstance(result, dict), "Некорректный документ в ответе.", "invalid_response")
        document = result.get("Документ", result)
        require(isinstance(document, dict) and document.get("Идентификатор") == identifier,
                "Сервер не вернул запрошенный документ.", "invalid_response")
        return document

    def list_documents(self, filters, all_pages=False, max_pages=DEFAULT_MAX_PAGES):
        require(type(max_pages) is int and max_pages > 0, "Лимит страниц должен быть положительным целым числом.")
        filters = copy.deepcopy(filters)
        require(filters.get("Тип"), "Для списка документов нужен Фильтр.Тип.")
        nav = filters.setdefault("Навигация", {})
        require(isinstance(nav, dict), "Навигация должна быть объектом.")
        try:
            page = int(nav.get("Страница", 0))
            size = int(nav.get("РазмерСтраницы", 20))
        except (ValueError, TypeError):
            raise ClientError("invalid_input", "Некорректная навигация.") from None
        require(page >= 0 and 1 <= size <= 200, "Страница >= 0, размер страницы от 1 до 200.")
        documents = []
        requests = 0
        while True:
            nav.update({"Страница": str(page), "РазмерСтраницы": str(size)})
            result = self.rpc("СБИС.СписокДокументов", {"Фильтр": filters})
            requests += 1
            require(isinstance(result, dict), "Некорректный список документов.", "invalid_response")
            rows = objects(result.get("Документ"))
            if not all_pages:
                return result
            documents.extend(rows)
            more = result.get("Навигация", {}).get("ЕстьЕще")
            require(more in ("Да", "Нет"), "Не получен признак следующей страницы.", "invalid_response")
            if more == "Нет":
                return {"Документ": documents, "Навигация": result["Навигация"], "all_pages": True}
            require(rows, "Остановлен некорректный обход страниц.", "invalid_response")
            require(requests < max_pages,
                    f"Достигнут лимит запросов страниц ({max_pages}). Полный список не получен; "
                    "увеличьте --max-pages или сузьте фильтр.", "pagination_limit")
            page += 1

    def get_file(self, info):
        require(isinstance(info, dict), "Нет сведений о файле.")
        if info.get("ДвоичныеДанные") is not None:
            try:
                data = base64.b64decode(info["ДвоичныеДанные"], validate=True)
            except (ValueError, TypeError):
                raise ClientError("invalid_input", "Некорректные данные Base64.") from None
            require(len(data) <= MAX_FILE, f"Файл превышает лимит клиента {format_size(MAX_FILE)}.")
            return data
        require(info.get("Ссылка"), "Нет ссылки или содержимого файла; сохранение вложений невозможно.")
        file_url(info["Ссылка"])
        try:
            return self.transport.get(info["Ссылка"])
        except urllib.error.HTTPError as exc:
            exc.close()
            self.http_error(exc.code)
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise ClientError("network_error", "Не удалось скачать файл. Ссылка и секреты скрыты.") from None

    def download(self, identifier, directory):
        document = self.read(identifier)
        require(document.get("ЧастичныеДанные") != "Да", "Получены частичные данные; полная выгрузка невозможна.", "partial_data")
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        saved = []
        for attachment in objects(document.get("Вложение")):
            entries = [("attachment", attachment.get("Файл"))]
            entries.extend(("signature", s.get("Файл")) for s in objects(attachment.get("Подпись")))
            for kind, info in entries:
                require(isinstance(info, dict), "Нет файла вложения или подписи.", "invalid_response")
                content = self.get_file(info)
                name = safe_name(info.get("Имя", "file.bin"))
                stem, suffix = os.path.splitext(name)
                index = 0
                while True:
                    path = root / (name if not index else f"{stem}_{index}{suffix}")
                    try:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        break
                    except FileExistsError:
                        index += 1
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(content)
                except OSError:
                    path.unlink(missing_ok=True)
                    raise
                saved.append({"kind": kind, "path": str(path), "bytes": len(content),
                              "attachment_id": attachment.get("Идентификатор")})
        return {"document_id": identifier, "files": saved}


def safe_name(name):
    name = str(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name).strip(" .")
    return name.encode("utf-8")[:180].decode("utf-8", errors="ignore") or "file.bin"
