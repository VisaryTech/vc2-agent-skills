"""Draft preparation and conservative reconstruction of a new copy."""
import base64
import binascii
import copy
import json
from pathlib import Path

from .common import MAX_BYTES, ClientError, box_guid, guid, require

MESSAGE_FIELDS = {"FromBoxId", "ToBoxId", "FromDepartmentId", "ToDepartmentId", "IsInternal",
                  "ProxyBoxId", "ProxyDepartmentId", "IsDraft", "LockDraft", "StrictDraftValidation",
                  "DelaySend", "LockPacket", "LockMode", "DocumentAttachments"}
ATTACHMENT_FIELDS = {"SignedContent", "Comment", "NeedRecipientSignature", "InitialDocumentIds",
                     "SubordinateDocumentIds", "CustomDocumentId", "NeedReceipt", "CustomData",
                     "TypeNamedId", "Function", "Version", "Metadata", "WorkflowId", "IsEncrypted",
                     "EditingSettingId", "ParticipantRoles", "FilePath"}
COPY_FIELDS = {"TypeNamedId", "Function", "Version", "Metadata", "WorkflowId", "CustomDocumentId",
               "CustomData", "InitialDocumentIds", "SubordinateDocumentIds", "EditingSettingId"}


def object_fields(value, allowed, context):
    require(isinstance(value, dict), f"{context}: требуется объект.")
    require(not set(value) - allowed, f"{context}: неподдерживаемые поля; проверьте справочник.")


def file_bytes(path):
    require(isinstance(path, str) and bool(path), "FilePath должен быть непустой строкой.")
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except OSError:
        raise ClientError("file_error", "Не удалось прочитать файл вложения.") from None
    require(len(data) <= MAX_BYTES, "Файл превышает лимит 100 МиБ.")
    return data


def prepare(payload, box_id):
    object_fields(payload, MESSAGE_FIELDS, "MessageToPost")
    result = copy.deepcopy(payload)
    require("IsDraft" not in result or result["IsDraft"] is True, "IsDraft должен быть true.")
    require("DelaySend" not in result or result["DelaySend"] is False, "DelaySend не поддерживается.")
    require(box_guid(result.get("FromBoxId")) == box_guid(box_id), "FromBoxId не соответствует --box-id.")
    for key in ("IsInternal", "LockDraft", "StrictDraftValidation", "LockPacket"):
        require(key not in result or type(result[key]) is bool, f"{key} должен быть boolean.")
    require(result.get("IsInternal") is not True or not result.get("ToBoxId"), "Внутренний черновик не должен содержать ToBoxId.")
    if result.get("ToBoxId"):
        require(box_guid(result["ToBoxId"]) != box_guid(box_id), "Ящики отправителя и получателя должны различаться.")
    attachments = result.get("DocumentAttachments")
    require(isinstance(attachments, list) and 1 <= len(attachments) <= 40,
            "Требуется от 1 до 40 DocumentAttachments.")
    custom_ids = set()
    for attachment in attachments:
        object_fields(attachment, ATTACHMENT_FIELDS, "DocumentAttachment")
        require(isinstance(attachment.get("TypeNamedId"), str) and bool(attachment["TypeNamedId"]), "Укажите TypeNamedId.")
        require(attachment.get("IsEncrypted", False) is False, "Зашифрованные вложения не поддерживаются.")
        for key in ("NeedReceipt", "NeedRecipientSignature"):
            require(key not in attachment or type(attachment[key]) is bool, f"{key} должен быть boolean.")
        if "CustomDocumentId" in attachment:
            cid = attachment["CustomDocumentId"]
            require(isinstance(cid, str) and cid and cid not in custom_ids, "CustomDocumentId должны быть непустыми и уникальными.")
            custom_ids.add(cid)
        content = attachment.setdefault("SignedContent", {})
        object_fields(content, {"Content"}, "SignedContent (подписи и полка не поддерживаются)")
        if "FilePath" in attachment:
            require("Content" not in content, "Укажите только FilePath либо SignedContent.Content.")
            path = attachment.pop("FilePath")
            content["Content"] = base64.b64encode(file_bytes(path)).decode("ascii")
        require(isinstance(content.get("Content"), str), "Укажите FilePath или SignedContent.Content.")
        try:
            data = base64.b64decode(content["Content"], validate=True)
        except (ValueError, binascii.Error):
            raise ClientError("invalid_input", "SignedContent.Content должен содержать Base64.") from None
        require(len(data) <= MAX_BYTES, "Вложение превышает лимит 100 МиБ.")
    result["IsDraft"] = True
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError):
        raise ClientError("invalid_input", "Запрос должен содержать корректные JSON-значения.") from None
    require(len(encoded) <= MAX_BYTES, "JSON-запрос превышает лимит 100 МиБ.")
    return result


def copy_draft(client, message_id, patch):
    """PATCH = {Message: {...}, Add: [...], Replace: {EntityId: {...}}, Remove: [...]}.

    Unknown child entities, encryption and internal document links cannot be silently
    discarded. Refuse them, including when their parent is requested for removal.
    """
    object_fields(patch, {"Message", "Add", "Replace", "Remove"}, "Patch")
    message_patch = patch.get("Message", {})
    object_fields(message_patch, MESSAGE_FIELDS - {"DocumentAttachments"}, "Patch.Message")
    additions, replacements, removals = patch.get("Add", []), patch.get("Replace", {}), patch.get("Remove", [])
    require(isinstance(additions, list) and isinstance(replacements, dict) and isinstance(removals, list),
            "Add и Remove должны быть массивами; Replace — объектом.")
    require(all(isinstance(x, str) for x in removals), "Remove должен содержать EntityId.")
    require(len(set(removals)) == len(removals) and not set(removals) & set(replacements), "Повторяющиеся или противоречивые EntityId.")
    message = client.message(message_id)
    is_draft = (message["MessageType"] == "Draft" if "MessageType" in message
                else message.get("IsDraft") is True)
    require(is_draft and message.get("DraftIsRecycled", False) is False
            and message.get("IsDeleted", False) is False
            and not message.get("DraftIsTransformedToMessageId")
            and not message.get("DraftIsTransformedToMessageIdList"), "Источник не является действующим черновиком.", "not_draft")
    require(box_guid(message.get("FromBoxId")) == client.box(), "Черновик принадлежит другому ящику.", "not_draft")
    require(not message.get("DraftIsLocked") and message.get("LockMode", "None") == "None"
            and not message.get("PacketIsLocked"), "Копирование заблокированного черновика не поддерживается.", "unsupported_copy")
    require(not message.get("IsTest") and not message.get("IsProxified") and not message.get("ProxyBoxId"),
            "Копирование тестового черновика или через промежуточного получателя не поддерживается.", "unsupported_copy")
    entities = message.get("Entities")
    require(isinstance(entities, list) and all(isinstance(e, dict) for e in entities), "Некорректный Entities.", "invalid_response")
    roots = [e for e in entities if e.get("EntityType") == "Attachment" and not e.get("ParentEntityId")]
    root_ids = {e.get("EntityId") for e in roots}
    require(len(root_ids) == len(roots) and all(isinstance(x, str) and x for x in root_ids), "Некорректные EntityId.", "invalid_response")
    require(set(replacements) | set(removals) <= root_ids, "Replace/Remove содержит неизвестный EntityId.")
    comments = {}
    for entity in entities:
        if entity in roots:
            continue
        parent = entity.get("ParentEntityId")
        require(entity.get("EntityType") == "Attachment" and entity.get("AttachmentType") == "AttachmentComment"
                and parent in root_ids and parent not in comments,
                "Невозможно сохранить подпись, служебную или неизвестную сущность при копировании.", "unsupported_copy")
        try:
            comments[parent] = client.content(message_id, entity["EntityId"]).decode("utf-8-sig")
        except UnicodeError:
            raise ClientError("unsupported_copy", "Комментарий не является UTF-8 текстом.") from None
    result = {k: copy.deepcopy(message[k]) for k in ("FromBoxId", "ToBoxId", "IsInternal", "ProxyBoxId") if message.get(k)}
    attachments = []
    departments = {"FromDepartmentId": set(), "ToDepartmentId": set()}
    for entity in roots:
        eid = entity["EntityId"]
        info = entity.get("DocumentInfo")
        require(isinstance(info, dict) and bool(info.get("TypeNamedId")), "Нет данных для восстановления вложения.", "unsupported_copy")
        require(not entity.get("IsEncryptedContent") and not info.get("IsEncryptedContent")
                and not info.get("IsDeleted") and not info.get("ParticipantRoles")
                and all(isinstance(p, dict) and isinstance(p.get("Participant"), dict)
                        and p["Participant"].get("Role") in ("default:sender", "default:recipient")
                        for p in info.get("Participants", [])), "Зашифрованные, удалённые или многотитульные вложения не копируются.", "unsupported_copy")
        for key in departments:
            # Missing/empty IDs and the zero UUID all denote the head department.
            department = guid(info[key]) if info.get(key) else "00000000-0000-0000-0000-000000000000"
            departments[key].add(department)
        attachment = {k: copy.deepcopy(info[k]) for k in COPY_FIELDS if k in info and info[k] != ""}
        for field in ("InitialDocumentIds", "SubordinateDocumentIds"):
            links = attachment.get(field, [])
            require(isinstance(links, list) and all(isinstance(x, dict) for x in links), "Некорректные связи документа.", "unsupported_copy")
            require(all(x.get("MessageId") and guid(x["MessageId"]) != guid(message_id) for x in links),
                    "Внутренние связи пакета нельзя перенести без изменения смысла.", "unsupported_copy")
        metadata = attachment.get("Metadata", [])
        require(isinstance(metadata, list) and all(isinstance(m, dict) for m in metadata),
                "Некорректные метаданные документа.", "unsupported_copy")
        if entity.get("FileName") and not any(m.get("Key") == "FileName" for m in metadata):
            attachment["Metadata"] = metadata + [{"Key": "FileName", "Value": entity["FileName"]}]
        for key in ("NeedRecipientSignature", "NeedReceipt"):
            if key in entity:
                attachment[key] = entity[key]
        if eid in comments:
            attachment["Comment"] = comments[eid]
        if eid in removals:
            continue
        replacement = replacements.get(eid, {})
        object_fields(replacement, ATTACHMENT_FIELDS, "Replace")
        if "FilePath" in replacement or "SignedContent" in replacement:
            attachment.pop("SignedContent", None)
        else:
            attachment["SignedContent"] = {"Content": base64.b64encode(client.content(message_id, eid)).decode("ascii")}
        attachment.update(copy.deepcopy(replacement))
        attachments.append(attachment)
    for key, values in departments.items():
        require(len(values) <= 1, "У вложений разные подразделения; копирование заблокировано.", "unsupported_copy")
        if values:
            result[key] = values.pop()
    result.update(copy.deepcopy(message_patch))
    result["DocumentAttachments"] = attachments + copy.deepcopy(additions)
    return prepare(result, client.box())
