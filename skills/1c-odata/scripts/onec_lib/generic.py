"""Metadata-driven OData v3 access and stateless single-object mutations."""
import base64
import datetime as dt
from decimal import Decimal, InvalidOperation, localcontext
import re
import urllib.parse as url
import uuid

from .metadata import Schema, parse_metadata
from .common import Error, WireNumber, require


class EntityClient:
    """Discover and validate physical entity fields directly from CSDL."""

    def __init__(self, transport, metadata=None):
        self.transport = transport
        self.schema = Schema(metadata if metadata is not None else parse_metadata(
            transport.request("GET", "$metadata", metadata=True)))

    def encode(self, entity, payload):
        require(isinstance(payload, dict), "Payload должен быть объектом.")
        return {key: self.value(self.schema.property(entity, key), value) for key, value in payload.items()}

    def value(self, prop, value):
        kind = prop["Type"]
        if value is None:
            require(prop.get("Nullable", "true").lower() != "false", "Поле не допускает null.")
            return None
        if kind == "Edm.Guid":
            try:
                return str(uuid.UUID(value))
            except (ValueError, TypeError, AttributeError):
                raise Error("invalid_input", "Некорректный GUID.") from None
        if kind in {"Edm.DateTime", "Edm.DateTimeOffset"}:
            require(isinstance(value, str) and bool(re.fullmatch(
                r"\d{4}-\d\d-\d\d(?:T\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)?)?", value)),
                "Требуется дата или ISO datetime с точностью до микросекунд.")
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise Error("invalid_input", "Некорректная дата.") from None
            require(kind != "Edm.DateTime" or parsed.tzinfo is None, "Edm.DateTime не допускает смещение.")
            require(kind != "Edm.DateTimeOffset" or parsed.tzinfo is not None,
                    "Edm.DateTimeOffset требует явное смещение.")
            return parsed.isoformat()
        if kind in {"Edm.Decimal", "Edm.Double", "Edm.Single"}:
            require(isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool), "Ожидалось число.")
            try:
                dec = Decimal(str(value))
                require(dec.is_finite(), "Число должно быть конечным.")
                precision, scale = prop.get("Precision", ""), prop.get("Scale", "")
                digits = dec.as_tuple().digits
                exponent = dec.as_tuple().exponent
                if scale.isdigit():
                    with localcontext() as context:
                        context.prec = max(len(digits) + abs(exponent) + 1, int(scale) + 1)
                        require(dec == dec.quantize(Decimal(1).scaleb(-int(scale))), "Превышен Scale.")
                if precision.isdigit():
                    # Decimal(p,s) reserves p-s places for the integer part.
                    integer = max(dec.adjusted() + 1, 0) if dec else 0
                    used = integer + (int(scale) if scale.isdigit() else max(-exponent, 0))
                    require(used <= int(precision), "Превышен Precision.")
                if kind in {"Edm.Double", "Edm.Single"}:
                    require(abs(dec) <= Decimal("1.7976931348623157e308" if kind == "Edm.Double" else "3.4028234663852886e38"),
                            "Число вне диапазона типа.")
                return format(dec, "f") if kind == "Edm.Decimal" else WireNumber(format(dec, "f"))
            except (InvalidOperation, ValueError, OverflowError):
                raise Error("invalid_input", "Некорректное число.") from None
        ranges = {"Edm.Byte": (0, 255), "Edm.SByte": (-128, 127),
                  "Edm.Int16": (-2**15, 2**15-1), "Edm.Int32": (-2**31, 2**31-1),
                  "Edm.Int64": (-2**63, 2**63-1)}
        if kind in ranges:
            if kind == "Edm.Int64" and isinstance(value, str) and re.fullmatch(r"-?\d+", value):
                value = int(value)
            require(type(value) is int and ranges[kind][0] <= value <= ranges[kind][1], "Целое число вне диапазона типа.")
            return str(value) if kind == "Edm.Int64" else value
        if kind == "Edm.Binary":
            require(isinstance(value, str), "Binary передаётся строкой base64.")
            try:
                decoded = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                raise Error("invalid_input", "Некорректный base64.") from None
            length = prop.get("MaxLength", "Max")
            require(not length.isdigit() or len(decoded) <= int(length), "Binary превышает MaxLength.")
            return value
        if kind == "Edm.Time":
            require(isinstance(value, str) and bool(re.fullmatch(r"PT(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?", value)),
                    "Edm.Time требует длительность PT…H…M…S.")
            return value
        if kind.startswith("Collection("):
            require(isinstance(value, list), "Ожидался массив.")
            return [self.value({"Type": kind[11:-1], "Nullable": "false"}, item) for item in value]
        if kind == "Edm.String":
            require(isinstance(value, str), "Ожидалась строка.")
            length = prop.get("MaxLength", "Max")
            require(not length.isdigit() or len(value) <= int(length), "Строка превышает MaxLength.")
            return value
        if kind == "Edm.Boolean":
            require(type(value) is bool, "Ожидалось boolean.")
            return value
        if kind in self.schema.data["types"]:
            spec = self.schema.type(kind)
            if spec["kind"] == "EnumType":
                require(value in spec["members"], "Неизвестное значение EnumType.")
                return value
            require(spec["kind"] == "ComplexType", "Ожидался вложенный ComplexType.", "unsupported_type")
            return self.encode(spec, value)
        raise Error("unsupported_type", "Тип поля не поддерживается клиентом.")

    def literal(self, prop, value):
        if value is None:
            self.value(prop, value)
            return "null"
        encoded, kind = self.value(prop, value), prop["Type"]
        prefixes = {"Edm.Guid": "guid", "Edm.DateTime": "datetime", "Edm.DateTimeOffset": "datetimeoffset",
                    "Edm.Time": "time", "Edm.Binary": "binary"}
        if kind in prefixes:
            if kind == "Edm.Binary":
                encoded = base64.b64decode(encoded).hex()
            return prefixes[kind] + "'" + encoded + "'"
        if kind == "Edm.String":
            return "'" + encoded.replace("'", "''") + "'"
        if kind == "Edm.Boolean":
            return "true" if encoded else "false"
        suffixes = {"Edm.Int64": "L", "Edm.Decimal": "M", "Edm.Double": "D", "Edm.Single": "f"}
        if kind in suffixes or kind in {"Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32"}:
            return str(encoded) + suffixes.get(kind, "")
        raise Error("unsupported_type", "Тип не поддерживается в ключе или фильтре.")

    def keys(self, entity, key):
        spec = self.schema.entity(entity)
        require(isinstance(key, dict) and bool(spec["keys"]) and set(key) == set(spec["keys"]),
                "Передайте все и только ключевые поля сущности.")
        require(all(value is not None for value in key.values()), "Ключ не может быть null.")
        return self.encode(spec, key)

    def address(self, entity, key=None):
        spec = self.schema.entity(entity)
        path = url.quote(entity, safe="")
        if key is not None:
            self.keys(entity, key)
            terms = [name + "=" + self.literal(spec["properties"][name], key[name]) for name in spec["keys"]]
            path += "(" + url.quote(",".join(terms), safe="=,'()") + ")"
        return path

    def entities(self, search=""):
        return [{"name": name, "type": spec["type"]} for name, spec in sorted(self.schema.data["entity_sets"].items())
                if search.casefold() in name.casefold()]

    def describe(self, entity):
        spec = self.schema.entity(entity)
        nested = {}
        def visit(kind):
            if kind.startswith("Collection("):
                kind = kind[11:-1]
            if kind in nested or kind not in self.schema.data["types"]:
                return
            nested[kind] = self.schema.type(kind)
            for prop in nested[kind]["properties"].values():
                visit(prop["Type"])
        for prop in spec["properties"].values():
            visit(prop["Type"])
        return {"entity_set": entity, **spec, "types": nested,
                "publication": self.schema.data["entity_sets"][entity], "write_access": "not_tested"}

    def equal(self, prop, actual, expected):
        kind = prop["Type"]
        if expected is None:
            return actual is None
        if kind.startswith("Collection("):
            return isinstance(actual, list) and len(actual) == len(expected) and all(
                self.equal({"Type": kind[11:-1]}, old, new) for old, new in zip(actual, expected))
        if kind in self.schema.data["types"] and self.schema.type(kind)["kind"] == "ComplexType":
            return self.matches(self.schema.type(kind), actual, expected)
        if kind in {"Edm.DateTime", "Edm.DateTimeOffset"}:
            if isinstance(actual, str) and actual.startswith("/Date("):
                match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", actual)
                if not match:
                    return False
                parsed = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(milliseconds=int(match[1]))
                actual = parsed.replace(tzinfo=None).isoformat() if kind == "Edm.DateTime" else parsed.isoformat()
            try:
                return dt.datetime.fromisoformat(self.value(prop, actual)) == dt.datetime.fromisoformat(self.value(prop, expected))
            except Error:
                return False
        if kind in {"Edm.Decimal", "Edm.Double", "Edm.Single", "Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64"}:
            try:
                return not isinstance(actual, bool) and Decimal(str(actual)) == Decimal(str(expected))
            except InvalidOperation:
                return False
        if kind == "Edm.Guid":
            return isinstance(actual, str) and actual.lower() == expected.lower()
        return type(actual) is type(expected) and actual == expected

    def matches(self, spec, actual, expected):
        return isinstance(actual, dict) and all(name in actual and self.equal(spec["properties"][name], actual[name], value)
                                                for name, value in expected.items())

    def get(self, entity, key, missing_ok=False):
        encoded = self.keys(entity, key)
        try:
            result = self.transport.request("GET", self.address(entity, key) + "?$format=json")
        except Error as exc:
            if missing_ok and exc.details.get("http_status") == 404:
                return None
            raise
        if isinstance(result, dict) and "d" in result:
            result = result["d"]
        require(self.matches(self.schema.entity(entity), result, encoded), "Ответ не соответствует ключу.", "invalid_response")
        return result

    def query_entity(self, entity, where=None, select=(), order_by=(), limit=100):
        spec = self.schema.entity(entity)
        require(where is None or isinstance(where, dict), "Условия должны быть объектом.")
        require(limit is None or type(limit) is int and 1 <= limit <= 10000, "Лимит должен быть 1–10000.")
        where = where or {}
        conditions = self.encode(spec, where)
        terms = [name + " eq " + self.literal(spec["properties"][name], value) for name, value in where.items()]
        params = {"$format": "json"}
        for name in select:
            self.schema.property(spec, name)
        if select:
            # Include filter fields for local verification; project them out below.
            params["$select"] = ",".join(dict.fromkeys([*select, *where]))
        ordering = []
        for term in order_by:
            pieces = term.split()
            require(len(pieces) in {1, 2} and (len(pieces) == 1 or pieces[1] in {"asc", "desc"}), "Некорректная сортировка.")
            self.schema.property(spec, pieces[0])
            ordering.append(" ".join(pieces))
        if ordering:
            params["$orderby"] = ",".join(ordering)
        if terms:
            params["$filter"] = " and ".join(terms)
        if limit is not None:
            params["$top"] = str(limit)
        address = self.address(entity) + "?" + url.urlencode(params, quote_via=url.quote)
        rows, visited = [], set()
        while address:
            target = self.transport.safe_url(address)
            require(target not in visited and len(visited) < 10000, "Циклическая или слишком длинная пагинация.", "invalid_response")
            visited.add(target)
            result = self.transport.request("GET", target)
            if isinstance(result, dict) and "d" in result:
                result = result["d"]
            if isinstance(result, list):
                page, following = result, None
            else:
                require(isinstance(result, dict), "Некорректный ответ коллекции.", "invalid_response")
                page = result.get("value", result.get("results"))
                following = result.get("odata.nextLink", result.get("@odata.nextLink", result.get("__next")))
            require(isinstance(page, list) and all(self.matches(spec, row, conditions) for row in page),
                    "Ответ не соответствует фильтру.", "invalid_response")
            rows.extend({k: v for k, v in row.items() if k in select} if select else row for row in page)
            require(len(rows) <= 100000, "Уточните фильтр: слишком много записей.", "query_too_large")
            if limit is not None and len(rows) >= limit:
                return rows[:limit]
            require(following is None or isinstance(following, str), "Некорректный nextLink.", "invalid_response")
            address = url.urljoin(target, following) if following else None
        return rows

    def check_write(self, entity, operation, payload, actual=None):
        spec = self.schema.entity(entity)
        declaration = self.schema.data["entity_sets"][entity]
        flag = {"create": "creatable", "update": "updatable", "mark-deletion": "updatable", "delete": "deletable"}[operation]
        require(not any(key.rsplit("}", 1)[-1].lower() == flag and str(value).lower() == "false"
                        for key, value in declaration["attributes"].items()), "Metadata запрещает операцию.", "write_forbidden")
        term, member = {"creatable": ("InsertRestrictions", "Insertable"), "updatable": ("UpdateRestrictions", "Updatable"),
                        "deletable": ("DeleteRestrictions", "Deletable")}[flag]
        for annotation in [*spec.get("annotations", []), *declaration.get("annotations", [])]:
            if annotation.get("Term", "").endswith("." + term):
                def denied(node):
                    return (node.get("Property") == member and (node.get("Bool") == "false" or any(
                        child.get("kind") == "Bool" and child.get("text") == "false" for child in node.get("children", [])))) or any(
                            denied(child) for child in node.get("children", []))
                require(not denied(annotation), "Metadata запрещает операцию.", "write_forbidden")
        if operation != "create":
            require(not set(payload) & set(spec["keys"]), "Изменение ключа запрещено.", "write_forbidden")
        posted = spec["properties"].get("Posted")
        if entity.startswith("Document_"):
            require(posted and posted["Type"] == "Edm.Boolean", "Нельзя определить состояние проведения документа.", "write_forbidden")
        if posted:
            require(posted["Type"] == "Edm.Boolean", "Неверный тип Posted.", "write_forbidden")
            require("Posted" not in payload or payload["Posted"] is False, "Проведение запрещено.", "write_forbidden")
            if operation == "create":
                payload["Posted"] = False
            else:
                require(actual is not None and actual.get("Posted") is False,
                        "Изменение проведённого документа или объекта с неизвестным Posted запрещено.", "write_forbidden")


class Mutations:
    """Single-object writes with immediate verification and no automatic replay."""

    def __init__(self, client):
        self.client = client

    def execute(self, entity, operation, data=None, key=None, dry_run=False):
        require(operation in {"create", "update", "mark-deletion", "delete"}, "Неизвестная операция.")
        spec = self.client.schema.entity(entity)
        require(bool(spec["keys"]), "Запись сущности без объявленных ключей не поддерживается.", "unsupported_type")
        payload = self.client.encode(spec, {} if data is None else data)
        if operation == "mark-deletion":
            prop = self.client.schema.property(spec, "DeletionMark")
            require(prop["Type"] == "Edm.Boolean", "DeletionMark должен быть Boolean.")
            require(set(payload) == {"DeletionMark"}, "Передайте только DeletionMark.")
        if operation == "delete":
            require(not payload, "DELETE не принимает тело.")
        if operation == "update":
            require(bool(payload), "Передайте обновляемые поля.")
        if operation == "create":
            require(key is None, "Ключ создания задаётся в payload.")
        else:
            key = self.client.keys(entity, key)
        actual = None if operation == "create" else self.client.get(entity, key, missing_ok=operation == "delete")
        require(operation == "create" or actual is not None, "Объект не найден.", "object_not_found")
        self.client.check_write(entity, operation, payload, actual)
        method = {"create": "POST", "update": "PATCH", "mark-deletion": "PATCH", "delete": "DELETE"}[operation]
        address = self.client.address(entity, key) + "?$format=json"
        if dry_run:
            return {"ok": True, "dry_run": True, "method": method,
                    "entity_set": entity, "key": key, "payload": payload, "write_access": "not_tested"}
        if operation == "create" and set(spec["keys"]) <= set(payload):
            key = self.client.keys(entity, {name: payload[name] for name in spec["keys"]})
        try:
            response = self.client.transport.request(method, address, None if method == "DELETE" else payload)
        except Error as exc:
            if exc.code == "write_outcome_unknown":
                raise Error(exc.code, "Результат записи неизвестен. Перед повтором проверьте объект в 1С.",
                            entity_set=entity, operation=operation, key=key, **exc.details) from None
            raise
        # A failure after a successful write is an uncertain write, not a safe-to-retry read error.
        try:
            if operation == "create":
                if isinstance(response, dict) and "d" in response:
                    response = response["d"]
                if isinstance(response, dict) and set(spec["keys"]) <= set(response):
                    key = self.client.keys(entity, {name: response[name] for name in spec["keys"]})
            require(key is not None, "Сервер не вернул ключ созданного объекта.", "missing_response_key")
            observed = self.client.get(entity, key, missing_ok=True)
            verified = observed is None if operation == "delete" else (
                observed is not None and self.client.matches(spec, observed, payload) and (
                    "Posted" not in spec["properties"] or observed.get("Posted") is False))
            require(verified, "Контрольное чтение не подтвердило запись.", "verification_failed")
        except Error as exc:
            raise Error("write_outcome_unknown", "Не удалось подтвердить запись. Перед повтором проверьте объект в 1С.",
                        entity_set=entity, operation=operation, key=key, verification_error=exc.code) from None
        return {"ok": True, "entity_set": entity, "operation": operation, "key": key, "status": "complete"}
