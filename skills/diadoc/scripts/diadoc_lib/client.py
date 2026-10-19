"""Read operations, content downloads and explicitly draft-only writes."""
import copy
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse

from .common import ClientError, box_guid, guid, require
from .transport import HttpTransport

HOST = "https://diadoc-api.kontur.ru"


class Client:
    def __init__(self, token="", box_id=None, transport=None, token_provider=None):
        self.token = token
        self.box_id = box_guid(box_id) if box_id is not None else None
        self.transport = transport or HttpTransport()
        self.token_provider = token_provider

    def box(self):
        require(self.box_id is not None, "Для операции требуется --box-id.")
        return self.box_id

    def call(self, path, query=None, payload=None, binary=False, operation_id=None):
        if self.token_provider is not None:
            self.token = self.token_provider.get_token()
        require(isinstance(self.token, str) and bool(self.token.strip()) and
                not any(c.isspace() for c in self.token),
                "Не получен непустой токен доступа без пробелов.", "credentials_required")
        url = HOST + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/octet-stream" if binary else "application/json",
                   "Content-Type": "application/json; charset=utf-8"}
        try:
            status, response_headers, body = self.transport.request(
                "POST" if payload is not None else "GET", url, payload, headers)
        except (OSError, urllib.error.URLError, HTTPException, ClientError) as exc:
            if operation_id:
                raise self.unknown(operation_id) from None
            if isinstance(exc, ClientError):
                raise
            raise ClientError("network_error", "Ошибка сети или таймаут; запрос не повторялся.") from None
        if operation_id and (status == 204 or any(k.lower() == "retry-after" for k in response_headers)):
            retry = next((v for k, v in response_headers.items() if k.lower() == "retry-after"), None)
            details = {"operation_id": operation_id}
            if isinstance(retry, str) and retry.isdecimal():
                details["retry_after_seconds"] = int(retry)
            raise ClientError("operation_pending", "Операция не завершена. Повторите тот же запрос с тем же operationId после Retry-After.", **details)
        if status != 200:
            if operation_id and status >= 500:
                raise self.unknown(operation_id)
            code = {401: "token_expired", 402: "subscription_required", 403: "access_denied", 404: "not_found"}.get(status, "http_error")
            raise ClientError(code, f"API вернул HTTP {status}; запрос не повторялся.", http_status=status)
        if binary:
            return body
        try:
            result = json.loads(body)
            require(isinstance(result, dict), "API вернул не объект.", "invalid_response")
            return result
        except (ValueError, UnicodeError, ClientError):
            if operation_id:
                raise self.unknown(operation_id) from None
            raise ClientError("invalid_response", "API вернул некорректный JSON-объект.") from None

    @staticmethod
    def unknown(operation_id):
        return ClientError("write_outcome_unknown", "Результат записи неизвестен. Повторяйте только тот же запрос с тем же operationId; новый ID может создать дубль.", operation_id=operation_id)

    def organizations(self):
        return self.call("/GetMyOrganizations")

    def documents(self, filters, all_pages=False, max_pages=10000):
        self.box()
        require(isinstance(filters, dict) and isinstance(filters.get("DocumentCategory"), str)
                and filters["DocumentCategory"] in {"Any", "Incoming", "Outgoing", "Internal", "Proxy", "Deleted"},
                "DocumentCategory: Any, Incoming, Outgoing, Internal, Proxy или Deleted.")
        count = filters.get("Count", 100)
        require(type(count) is int and 1 <= count <= 100, "Count должен быть от 1 до 100.")
        require(type(max_pages) is int and max_pages > 0, "max-pages должен быть положительным.")
        request = copy.deepcopy(filters)
        merged, seen = [], {request.get("AfterIndexKey", "")}
        for _ in range(max_pages if all_pages else 1):
            page = self.call("/V4/GetDocuments", {"boxId": self.box_id}, request)
            docs = page.get("Documents", [])
            require(isinstance(docs, list) and all(isinstance(d, dict) for d in docs), "Некорректный Documents.", "invalid_response")
            if not all_pages:
                return page
            merged.extend(docs)
            if len(docs) < count:
                return {**page, "Documents": merged, "all_pages": True}
            cursor = docs[-1].get("IndexKey")
            require(isinstance(cursor, str) and cursor and cursor not in seen,
                    "Курсор отсутствует или повторился.", "pagination_stalled")
            seen.add(cursor)
            request["AfterIndexKey"] = cursor
        raise ClientError("pagination_limit", "Достигнут max-pages; сузьте фильтр или увеличьте лимит.")

    def message(self, message_id, entity_id=None):
        query = {"boxId": self.box(), "messageId": guid(message_id), "injectEntityContent": "false"}
        if entity_id:
            query["entityId"] = guid(entity_id)
        result = self.call("/V6/GetMessage", query)
        require(isinstance(result.get("MessageId"), str), "В ответе нет MessageId.", "invalid_response")
        return result

    def read(self, message_id, entity_id=None):
        message = self.message(message_id, entity_id)
        if not entity_id:
            return message
        document = self.call("/V3/GetDocument", {"boxId": self.box(), "messageId": guid(message_id),
                             "entityId": guid(entity_id), "injectEntityContent": "false"})
        return {"Message": message, "Document": document}

    def content(self, message_id, entity_id):
        return self.call("/V4/GetEntityContent", {"boxId": self.box(), "messageId": guid(message_id),
                         "entityId": guid(entity_id)}, binary=True)

    def download(self, message_id, output, entity_id=None):
        entity_id = guid(entity_id) if entity_id else None
        message = self.message(message_id, entity_id)
        entities = message.get("Entities", [])
        require(isinstance(entities, list) and all(isinstance(e, dict) for e in entities), "Некорректный Entities.", "invalid_response")
        if entity_id:
            require(any(e.get("EntityId") == entity_id for e in entities), "Сущность не найдена.", "not_found")
        root = Path(output)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        files = []
        for entity in entities:
            if entity.get("EntityType") not in ("Attachment", "Signature"):
                continue
            eid = guid(entity.get("EntityId"))
            data = self.content(message_id, eid)
            raw = entity.get("FileName") or (eid + (".sig" if entity.get("EntityType") == "Signature" else ".bin"))
            name = re.sub(r'[\x00-\x1f\x7f/:\\]', "_", str(raw)).strip(". ")
            # Bound bytes, not characters, leaving room for the collision prefix.
            name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(". ") or eid
            for suffix in range(10000):
                path = root / (name if suffix == 0 else f"{suffix}_{name}")
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    break
                except FileExistsError:
                    continue
            else:
                raise ClientError("file_error", "Не удалось выбрать свободное имя файла.")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
            except OSError:
                path.unlink(missing_ok=True)
                raise
            files.append({"entity_id": eid, "path": str(path.resolve()), "bytes": len(data)})
        return {"message_id": message_id, "files": files}

    def post_draft(self, payload, operation_id):
        require(payload.get("IsDraft") is True, "Разрешена только запись черновика.")
        result = self.call("/V3/PostMessage", {"operationId": guid(operation_id)}, payload, operation_id=guid(operation_id))
        if not isinstance(result.get("MessageId"), str) or not result["MessageId"]:
            raise self.unknown(operation_id)
        return result
