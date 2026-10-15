"""Preparation of outgoing drafts with attachment preservation."""

import base64
import copy
from pathlib import Path

from .common import MAX_FILE, format_size, objects, require


WRITE_FIELDS = {
    "Идентификатор", "Номер", "Дата", "Сумма", "Срок", "Примечание", "Тип",
    "Подтип", "Регламент", "НашаОрганизация", "Контрагент", "Подразделение",
    "Ответственный", "Автор", "НачалоПериода", "КонецПериода", "Вложение",
    "ДокументОснование", "ДокументСледствие",
}


ATTACHMENT_FIELDS = {
    "Идентификатор", "Название", "Дата", "Номер", "Сумма", "Файл", "Подпись",
    "Тип", "Подтип", "ВерсияФормата", "ПодверсияФормата", "Служебный",
    "Зашифрован", "ТипШифрования",
}


POWER_OF_ATTORNEY_FIELDS = {
    "ИдентификаторМЧД", "СистемаХраненияМЧД", "ДействительнаС", "ДействительнаПо",
    "ВнутрНомер", "ИННДоверителя", "ИННПредставителя",
}


def require_unlocked(document):
    extension = document.get("Расширение")
    require(isinstance(extension, dict) and extension.get("ЗакрытОтИзменений") == "Нет",
            "Документ закрыт от изменений или разрешение изменений не подтверждено.", "not_draft")


def merge(base, patch):
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def local_attachment(path, identifier=None):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= MAX_FILE,
            f"Вложение отсутствует или больше {format_size(MAX_FILE)}.")
    data = path.read_bytes()
    require(len(data) <= MAX_FILE, f"Вложение больше {format_size(MAX_FILE)}.")
    result = {"Файл": {"Имя": path.name, "ДвоичныеДанные": base64.b64encode(data).decode("ascii")}}
    if identifier:
        result["Идентификатор"] = identifier
    return result


def prepare_draft(client, patch, identifier=None, additions=(), replacements=(), removals=()):
    require(isinstance(patch, dict), "Документ должен быть JSON-объектом.")
    require(not (set(patch) - WRITE_FIELDS), "В JSON есть поля, не поддерживаемые записью черновика.")
    require("Идентификатор" not in patch, "Идентификатор задаётся аргументом draft-update, а не JSON.")
    if identifier:
        current = client.read(identifier, include_extension=True)
        state = current.get("Состояние", {})
        require(isinstance(state, dict) and str(state.get("Код")) == "0"
                and current.get("Направление") == "Исходящий"
                and current.get("Удален", "Нет") == "Нет",
                "Обновление разрешено только для исходящего черновика с кодом состояния 0.", "not_draft")
        require(current.get("ЧастичныеДанные") != "Да", "Нельзя обновить документ с частичными данными.", "partial_data")
        require_unlocked(current)
        client.draft_snapshot = copy.deepcopy(current)
        base = {k: copy.deepcopy(v) for k, v in current.items() if k in WRITE_FIELDS}
        existing = objects(base.pop("Вложение", None))
    else:
        require(not replacements and not removals, "Заменять и удалять вложения можно только при обновлении.")
        base, existing = {}, []
    patch = copy.deepcopy(patch)
    incoming = objects(patch.pop("Вложение", None))
    document = merge(base, patch)
    require(document.get("Тип"), "Укажите Тип документа явно.")
    attachments = [{k: v for k, v in a.items() if k in ATTACHMENT_FIELDS} for a in existing]
    known = {a.get("Идентификатор") for a in attachments}
    require(None not in known, "У существующего вложения нет идентификатора.", "invalid_response")
    require(len(known) == len(attachments), "Повторяющиеся идентификаторы вложений.", "invalid_response")
    touched = set()
    def replace(item):
        aid = item.get("Идентификатор")
        require(aid in known and aid not in touched, "Неизвестное или повторно изменяемое вложение.")
        touched.add(aid)
        index = next(i for i, a in enumerate(attachments) if a.get("Идентификатор") == aid)
        old = attachments[index]
        attachments[index] = merge(old, item)
        if "Файл" in item:
            # Replacement bytes invalidate old signatures and old file URLs.
            attachments[index]["Файл"] = item["Файл"]
            attachments[index].pop("Подпись", None)
    for aid in removals:
        require(aid in known and aid not in touched, "Неизвестное или повторно изменяемое вложение.")
        touched.add(aid)
        attachments = [a for a in attachments if a.get("Идентификатор") != aid]
    for spec in replacements:
        aid, sep, path = spec.partition("=")
        require(sep and aid and path, "Формат замены: ID=PATH.")
        replace(local_attachment(path, aid))
    for item in incoming:
        require(not (set(item) - ATTACHMENT_FIELDS) and "Подпись" not in item,
                "Неподдерживаемые поля вложения; загрузка новых подписей не входит в v1.")
        if item.get("Идентификатор"):
            replace(item)
        else:
            attachments.append(item)
    attachments.extend(local_attachment(path) for path in additions)
    require(len(attachments) <= 10, "В документе допускается не более 10 вложений.")
    for item in attachments:
        require(isinstance(item.get("Файл"), dict), "Для вложения нужен объект Файл.")
        item["Файл"] = materialize(client, item["Файл"])
        if "Подпись" in item:
            item["Подпись"] = [materialize_signature(client, s) for s in objects(item["Подпись"])]
    document["Вложение"] = attachments
    return document


def materialize_signature(client, signature):
    result = {"Файл": materialize(client, signature.get("Файл"))}
    certificate = signature.get("Сертификат")
    if isinstance(certificate, dict) and "Доверенность" in certificate:
        result["Сертификат"] = {"Доверенность": [
            {k: copy.deepcopy(v) for k, v in attorney.items() if k in POWER_OF_ATTORNEY_FIELDS}
            for attorney in objects(certificate["Доверенность"])
        ]}
    return result


def materialize(client, info):
    require(isinstance(info, dict) and info.get("Имя"), "Для файла нужно Имя.")
    require(not (info.get("Ссылка") and info.get("ДвоичныеДанные") is not None),
            "Ссылка и ДвоичныеДанные взаимоисключающие.")
    data = client.get_file(info)
    return {"Имя": info["Имя"], "ДвоичныеДанные": base64.b64encode(data).decode("ascii")}
