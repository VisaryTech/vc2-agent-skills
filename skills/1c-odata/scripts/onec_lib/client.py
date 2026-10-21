"""Typed OData queries and a narrow collection-only create interface."""
from decimal import Decimal, InvalidOperation
import re
import urllib.parse as url
from .common import Error, WireNumber, canonical, date, guid, require
from .metadata import Schema, parse_metadata

READ_ROLES = {"organizations", "counterparties", "contracts", "items", "units", "currencies", "receipts", "invoices"}
WRITE_ROLES = {"counterparties", "receipts", "invoices"}
REQUIRED = {
    "organizations": {"id", "code", "inn", "kpp"},
    "counterparties": {"id", "inn", "kpp", "name"},
    "contracts": {"id", "code", "organization", "supplier"},
    "items": {"id", "code"}, "units": {"id", "code"}, "currencies": {"id", "code"},
    "receipts": {"id", "organization", "supplier", "number", "date", "total", "posted", "includes_vat"},
    "invoices": {"id", "organization", "supplier", "number", "date", "posted"},
}
ALLOWED = {role: required | {
    "organizations": {"name"}, "counterparties": {"kind"}, "contracts": {"name"},
    "items": {"name", "unit", "is_service", "is_folder"}, "units": {"name"}, "currencies": {"name"},
    "receipts": {"contract", "currency", "vat_total", "deletion_mark", "document_kind", "document_date", "is_upd"},
    "invoices": {"basis", "contract", "currency", "total", "vat_total", "deletion_mark", "document_date"},
}[role] | {"deletion_mark"} for role, required in REQUIRED.items()}
LINE_REQUIRED = {"item", "unit", "quantity", "price", "amount", "vat", "vat_rate"}
LINE_ALLOWED = LINE_REQUIRED | {"line_no"}
GUID_FIELDS = {"id", "organization", "supplier", "contract", "currency", "basis", "item", "unit"}
DECIMAL_FIELDS = {"total", "vat_total", "quantity", "price", "amount", "vat"}


def literal(value, kind):
    if kind == "Edm.Guid":
        return "guid'" + guid(value) + "'"
    if kind == "Edm.DateTime":
        return "datetime'" + date(value) + "T00:00:00'"
    if kind == "Edm.DateTimeOffset":
        return "datetimeoffset'" + date(value) + "T00:00:00Z'"
    if kind == "Edm.Boolean":
        require(type(value) is bool, "Ожидалось boolean.")
        return "true" if value else "false"
    if kind.startswith("Edm.Int") or kind in {"Edm.Decimal", "Edm.Double"}:
        try:
            val = Decimal(str(value))
            require(val.is_finite(), "Число должно быть конечным.")
            return str(val)
        except InvalidOperation:
            raise Error("invalid_input", "Некорректное число фильтра.") from None
    require(kind == "Edm.String" and isinstance(value, str), "Неподдерживаемый тип фильтра.")
    return "'" + value.replace("'", "''") + "'"


class Client:
    def __init__(self, transport, metadata=None, profile=None):
        self.transport = transport
        self.schema = Schema(metadata or parse_metadata(transport.request("GET", "$metadata", metadata=True)))
        self.profile = profile
        if profile is not None:
            self.validate_profile()

    def validate_profile(self):
        p = self.profile
        require(isinstance(p, dict) and p.get("version") == 1 and p.get("reviewed") is True,
                "Нужен проверенный профиль version=1, reviewed=true.", "invalid_profile")
        require(p.get("metadata_sha256") == self.schema.data["sha256"], "Metadata изменилась; повторите discovery и проверку профиля.", "schema_changed")
        roles = p.get("roles", {})
        require(set(REQUIRED) - {"currencies"} <= set(roles) <= READ_ROLES, "Неверный набор ролей профиля.", "invalid_profile")
        require(len({r.get("entity_set") for r in roles.values()}) == len(roles),
                "Роли должны ссылаться на разные EntitySet.", "invalid_profile")
        for role, spec in roles.items():
            entity = self.schema.entity(spec.get("entity_set"))
            mapping = spec.get("fields", {})
            require(REQUIRED[role] <= set(mapping) <= ALLOWED[role], "Неверные поля роли " + role, "invalid_profile")
            self.validate_mapping(entity, mapping)
            require(entity["keys"] == [mapping["id"]], "Нужен единственный GUID-ключ сущности.", "invalid_profile")
            require(not (set(spec.get("defaults", {})) & set(mapping.values())), "Defaults пересекаются с полями роли.", "invalid_profile")
            self.validate_defaults(entity, spec.get("defaults", {}))
            mandatory = spec.get("required_on_create", [])
            require(isinstance(mandatory, list) and all(isinstance(k, str) and k in entity["properties"] for k in mandatory),
                    "required_on_create содержит неизвестные поля.", "invalid_profile")
            if role in {"receipts", "invoices"}:
                require(entity["properties"][mapping["posted"]]["Type"] == "Edm.Boolean", "Поле posted должно быть boolean.", "invalid_profile")
            if role in WRITE_ROLES:
                attributes = self.schema.data["entity_sets"][spec["entity_set"]]["attributes"]
                require(not any(k.rsplit("}", 1)[-1].lower() in {"creatable", "insertable"} and v.lower() == "false"
                                for k, v in attributes.items()), "Metadata запрещает создание роли " + role, "invalid_profile")
            if role == "invoices":
                require(("basis" in mapping) != ("basis_table" in spec), "Задайте basis либо basis_table счёта-фактуры.", "invalid_profile")
                if "basis_table" in spec:
                    table = spec["basis_table"]
                    kind = self.schema.property(entity, table.get("property"))["Type"]
                    require(kind.startswith("Collection("), "basis_table должна быть коллекцией.", "invalid_profile")
                    row = self.schema.type(kind[11:-1])
                    require({"basis"} <= set(table.get("fields", {})) <= {"basis", "line_no"}, "Неверные поля basis_table.", "invalid_profile")
                    require(row["kind"] == "ComplexType", "Табличная часть должна содержать ComplexType.", "invalid_profile")
                    self.validate_mapping(row, table["fields"])
                    require(table["property"] not in set(mapping.values()) | set(spec.get("defaults", {})), "basis_table пересекается с полями.", "invalid_profile")
                    require(not set(table.get("defaults", {})) & set(table["fields"].values()), "Defaults основания пересекаются с полями.", "invalid_profile")
                    self.validate_defaults(row, table.get("defaults", {}))
            if role == "receipts":
                tables = spec.get("tables", {})
                require(set(tables) == {"goods", "services"}, "Задайте goods и services таблицы поступления.", "invalid_profile")
                for table in tables.values():
                    prop = self.schema.property(entity, table.get("property"))
                    kind = prop["Type"]
                    require(kind.startswith("Collection("), "Табличная часть должна быть Collection.", "invalid_profile")
                    row = self.schema.type(kind[11:-1])
                    minimum = LINE_REQUIRED - ({"unit"} if table is tables["services"] else set())
                    require(minimum <= set(table.get("fields", {})) <= LINE_ALLOWED, "Неверные поля строки.", "invalid_profile")
                    require(row["kind"] == "ComplexType", "Табличная часть должна содержать ComplexType.", "invalid_profile")
                    self.validate_mapping(row, table["fields"])
                    require(table.get("amount_mode") in {"net", "gross", "input"}, "Укажите amount_mode строки.", "invalid_profile")
                    require(not set(table.get("defaults", {})) & set(table["fields"].values()), "Defaults строки пересекаются с её полями.", "invalid_profile")
                    self.validate_defaults(row, table.get("defaults", {}))
                require(not {t["property"] for t in tables.values()} & (set(mapping.values()) | set(spec.get("defaults", {}))),
                        "Таблицы пересекаются с полями документа.", "invalid_profile")
        rates = p.get("vat_rates", {})
        require(isinstance(rates, dict) and rates and set(rates) <= {"none", "0", "5", "7", "10", "18", "20", "22"}, "Неверный справочник ставок НДС.", "invalid_profile")
        require(all(value is not None and value != "" for value in rates.values())
                and len({canonical(v) for v in rates.values()}) == len(rates),
                "Ставки НДС должны иметь непустые различные значения.", "invalid_profile")
        for table in roles["receipts"]["tables"].values():
            row = self.row_schema(table)
            prop = self.schema.property(row, table["fields"]["vat_rate"])
            for value in rates.values():
                self.value(prop, value)
        tables_by_property = {}
        for table in roles["receipts"]["tables"].values():
            existing = tables_by_property.get(table["property"])
            require(existing is None or existing == table, "Одна табличная часть должна иметь одинаковое описание goods/services.", "invalid_profile")
            tables_by_property[table["property"]] = table
        if "currency" in roles["receipts"]["fields"]:
            require("currencies" in roles, "Для валютной ссылки нужна роль currencies.", "invalid_profile")

    def validate_defaults(self, entity, defaults):
        require(isinstance(defaults, dict), "Defaults должны быть объектом.", "invalid_profile")
        for name in defaults:
            kind = self.schema.property(entity, name)["Type"]
            require(kind.startswith("Edm.") or (kind in self.schema.data["types"] and self.schema.type(kind)["kind"] == "EnumType"),
                    "Defaults допускают только скалярные реквизиты.", "invalid_profile")
        self.encode(entity, defaults)

    def validate_mapping(self, entity, mapping):
        require(len(set(mapping.values())) == len(mapping), "Одно поле сопоставлено нескольким понятиям.", "invalid_profile")
        for logical, actual in mapping.items():
            prop = self.schema.property(entity, actual)
            kind = prop["Type"]
            if logical in GUID_FIELDS:
                require(kind == "Edm.Guid" or (logical == "basis" and kind == "Edm.String"), "Ссылка должна быть GUID; составное основание также допускает строковый UUID.", "invalid_profile")
            elif logical in DECIMAL_FIELDS:
                require(kind in {"Edm.Decimal", "Edm.Double"}, "Суммы и количества должны иметь тип Edm.Decimal/Edm.Double.", "invalid_profile")
            elif logical in {"posted", "includes_vat", "deletion_mark", "is_upd", "is_service", "is_folder"}:
                require(kind == "Edm.Boolean", "Ожидалось boolean поле.", "invalid_profile")
            elif logical in {"date", "document_date"}:
                require(kind in {"Edm.DateTime", "Edm.DateTimeOffset"}, "Ожидалось поле даты.", "invalid_profile")
            elif logical in {"inn", "kpp", "name", "code", "number"}:
                require(kind == "Edm.String", "Ожидалось строковое поле.", "invalid_profile")

    def row_schema(self, table):
        entity = self.schema.entity(self.role("receipts")["entity_set"])
        return self.schema.type(entity["properties"][table["property"]]["Type"][11:-1])

    def role(self, role):
        require(self.profile is not None and role in self.profile["roles"], "Роль отсутствует в профиле.", "invalid_profile")
        return self.profile["roles"][role]

    def value(self, prop, value):
        kind = prop["Type"]
        if value is None:
            require(prop.get("Nullable", "true") != "false", "Обязательное поле не может быть null.")
            return None
        if kind == "Edm.Guid":
            return guid(value)
        if kind in {"Edm.Decimal", "Edm.Double"}:
            try:
                dec = Decimal(str(value))
                require(dec.is_finite(), "Неконечное число.")
                scale = prop.get("Scale")
                if scale and scale.isdigit():
                    require(dec == dec.quantize(Decimal(1).scaleb(-int(scale))), "Точность поля не допускает такое число.")
                precision = prop.get("Precision")
                if precision and precision.isdigit():
                    require(len(dec.as_tuple().digits) + max(dec.as_tuple().exponent, 0) <= int(precision), "Переполнение точности поля.")
                return WireNumber(format(dec, "f")) if kind == "Edm.Double" else format(dec, "f")
            except (InvalidOperation, ValueError):
                raise Error("invalid_input", "Некорректное decimal поле.") from None
        if kind in {"Edm.DateTime", "Edm.DateTimeOffset"}:
            return date(value) + ("T00:00:00Z" if kind.endswith("Offset") else "T00:00:00")
        if kind == "Edm.String":
            require(isinstance(value, str), "Ожидалась строка.")
            limit = prop.get("MaxLength", "Max")
            require(not limit.isdigit() or len(value) <= int(limit), "Строка превышает MaxLength.")
            return value
        if kind == "Edm.Boolean":
            require(type(value) is bool, "Ожидалось boolean.")
            return value
        if kind.startswith("Edm.Int") or kind == "Edm.Byte":
            require(type(value) is int, "Ожидалось целое число.")
            return value
        if kind.startswith("Collection("):
            require(isinstance(value, list), "Ожидалась табличная часть.")
            row = self.schema.type(kind[11:-1])
            return [self.encode(row, v) for v in value]
        if kind in self.schema.data["types"]:
            spec = self.schema.type(kind)
            if spec["kind"] == "EnumType":
                require(value in spec["members"], "Неизвестное значение EnumType.")
                return value
            return self.encode(spec, value)
        raise Error("unsupported_type", "Тип поля не поддерживается клиентом.")

    def encode(self, entity, payload):
        require(isinstance(payload, dict), "Payload должен быть объектом.")
        return {key: self.value(self.schema.property(entity, key), value) for key, value in payload.items()}

    def payload(self, role, logical):
        spec = self.role(role)
        require(set(logical) <= set(spec["fields"]), "Реквизит не сопоставлен в профиле.", "invalid_profile")
        body = {**spec.get("defaults", {}), **{spec["fields"][k]: v for k, v in logical.items()}}
        return self.encode(self.schema.entity(spec["entity_set"]), body)

    def query(self, role, conditions=None, limit=None):
        spec = self.role(role)
        entity = self.schema.entity(spec["entity_set"])
        terms = []
        for key, value in (conditions or {}).items():
            require(key in spec["fields"], "Поле фильтра не сопоставлено.", "invalid_profile")
            actual = spec["fields"][key]
            terms.append(actual + " eq " + literal(value, entity["properties"][actual]["Type"]))
        params = {"$format": "json"}
        if limit is not None:
            params["$top"] = str(limit)
        if terms:
            params["$filter"] = " and ".join(terms)
        address = url.quote(spec["entity_set"], safe="") + "?" + url.urlencode(params, quote_via=url.quote)
        rows, visited = [], set()
        while address:
            target = self.transport.safe_url(address)
            require(target not in visited and len(visited) < 10000, "Циклическая или слишком длинная пагинация.", "invalid_response")
            visited.add(target)
            result = self.transport.request("GET", address)
            if isinstance(result, dict) and "d" in result:
                result = result["d"]
            if isinstance(result, list):
                page, following = result, None
            else:
                require(isinstance(result, dict), "Неверный ответ коллекции.", "invalid_response")
                page = result.get("value", result.get("results"))
                following = result.get("odata.nextLink", result.get("@odata.nextLink", result.get("__next")))
            require(isinstance(page, list) and all(isinstance(r, dict) for r in page), "Неверный ответ коллекции.", "invalid_response")
            for row in page:
                for key, value in (conditions or {}).items():
                    actual = spec["fields"][key]
                    kind = entity["properties"][actual]["Type"]
                    observed = row.get(actual)
                    if kind == "Edm.Guid":
                        equal = isinstance(observed, str) and observed.lower() == guid(value)
                    elif kind in {"Edm.DateTime", "Edm.DateTimeOffset"}:
                        if isinstance(observed, str) and observed.startswith("/Date("):
                            import datetime as dt
                            match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", observed)
                            require(bool(match), "Неверная дата ответа.", "invalid_response")
                            observed = dt.datetime.fromtimestamp(int(match[1]) / 1000, dt.timezone.utc).date().isoformat()
                        equal = isinstance(observed, str) and observed[:10] == date(value)
                    elif kind in {"Edm.Decimal", "Edm.Double"} or kind.startswith("Edm.Int"):
                        try:
                            equal = Decimal(str(observed)) == Decimal(str(value))
                        except InvalidOperation:
                            equal = False
                    elif kind == "Edm.Boolean":
                        equal = type(observed) is bool and observed == value
                    else:
                        equal = isinstance(observed, str) and observed == value
                    require(equal, "Сервер вернул запись, не соответствующую фильтру.", "invalid_response")
            rows.extend(page)
            require(len(rows) <= 100000, "Слишком много результатов; уточните фильтр.", "query_too_large")
            if limit is not None and len(rows) >= limit:
                return rows[:limit]
            require(following is None or isinstance(following, str), "Неверный nextLink.", "invalid_response")
            address = url.urljoin(target, following) if following else None
        return rows

    def read(self, role, identity):
        spec = self.role(role)
        key = guid(identity)
        result = self.transport.request("GET", url.quote(spec["entity_set"], safe="") + "(guid'" + key + "')?$format=json")
        if isinstance(result, dict) and "d" in result:
            result = result["d"]
        require(isinstance(result, dict) and result.get(spec["fields"]["id"]) == key, "Не удалось подтвердить объект по ID.", "invalid_response")
        return result

    def check_required(self, role, body, deferred=()):
        required = self.role(role).get("required_on_create", [])
        missing = [key for key in required if key not in deferred and (key not in body or body[key] is None or body[key] == "")]
        require(not missing, "Не заполнены обязательные реквизиты создания роли " + role, "missing_data")

    def create(self, role, body):
        require(role in WRITE_ROLES, "Создание этой роли запрещено.", "write_forbidden")
        spec = self.role(role)
        entity = self.schema.entity(spec["entity_set"])
        self.check_required(role, body)
        # The caller supplies an already encoded payload; validate keys and safety flags here.
        require(set(body) <= set(entity["properties"]), "Поле записи отсутствует в metadata.", "invalid_profile")
        require(spec["fields"]["id"] not in body, "Назначать ID при создании запрещено.", "write_forbidden")
        if role != "counterparties":
            require(body.get(spec["fields"]["posted"]) is False, "Создание допускается только без проведения.", "write_forbidden")
            if "deletion_mark" in spec["fields"]:
                require(body.get(spec["fields"]["deletion_mark"]) is False, "DeletionMark должен быть false.", "write_forbidden")
        return self.transport.request("POST", url.quote(spec["entity_set"], safe="") + "?$format=json", body)
