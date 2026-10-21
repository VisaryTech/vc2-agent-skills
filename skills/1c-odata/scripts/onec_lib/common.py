"""Strict input primitives and structured, non-sensitive errors."""
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
import uuid

MAX_BYTES = 16 * 1024 * 1024


class Error(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def result(self):
        return {"ok": False, "error": self.code, "message": self.message, **self.details}


def require(condition, message, code="invalid_input"):
    if not condition:
        raise Error(code, message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_json(path):
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, "JSON превышает лимит 16 МиБ.")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, "Повторяющийся ключ JSON.")
                result[key] = value
            return result
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (OSError, ValueError, UnicodeError):
        raise Error("file_error", "Не удалось прочитать корректный JSON.") from None


def fields(value, allowed, required=()):
    require(isinstance(value, dict), "Требуется JSON-объект.")
    require(not set(value) - set(allowed), "Неизвестные поля входного объекта.")
    require(set(required) <= set(value), "Не заполнены обязательные поля: " + ", ".join(sorted(set(required) - set(value))))


def guid(value):
    try:
        result = str(uuid.UUID(value))
        require(result != str(uuid.UUID(int=0)), "Пустой UUID недопустим.")
        return result
    except (ValueError, TypeError, AttributeError):
        raise Error("invalid_input", "Требуется непустой UUID.") from None


def date(value):
    require(isinstance(value, str), "Дата должна быть строкой YYYY-MM-DD.")
    try:
        result = dt.date.fromisoformat(value)
        require(result.isoformat() == value, "Требуется дата YYYY-MM-DD.")
        return value
    except ValueError:
        raise Error("invalid_input", "Требуется дата YYYY-MM-DD.") from None


def decimal(value, money=False):
    require(isinstance(value, str) and bool(re.fullmatch(r"-?\d+(?:\.\d+)?", value)),
            "Числа количества, цены и суммы передавайте десятичными строками.")
    try:
        result = Decimal(value)
        require(result.is_finite() and result >= 0 and result < Decimal("1e15"), "Число вне допустимого диапазона.")
        if money:
            require(result == result.quantize(Decimal("0.01")), "Денежная сумма должна иметь точность до копеек.")
        return result
    except InvalidOperation:
        raise Error("invalid_input", "Некорректное десятичное число.") from None


def rounded(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def tax_id(value):
    fields(value, {"inn", "kpp", "name", "kind"}, {"inn"})
    inn, kpp = value["inn"], value.get("kpp", "")
    require(isinstance(inn, str) and bool(re.fullmatch(r"\d{10}|\d{12}", inn)), "ИНН: 10 или 12 цифр.")
    require(isinstance(kpp, str) and (bool(re.fullmatch(r"\d{9}", kpp)) if len(inn) == 10 else kpp == ""),
            "Для организации нужен КПП из 9 цифр; для ИП КПП не указывается.")
    return inn, kpp


class WireNumber(str):
    """Decimal text emitted as a JSON number for CSDL Edm.Double fields."""


def wire_json(value):
    if isinstance(value, WireNumber):
        require(bool(re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value)), "Некорректное число OData.")
        require(Decimal(value).is_finite(), "Неконечное число OData.")
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(canonical(k) + ":" + wire_json(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(wire_json(v) for v in value) + "]"
    return canonical(value)
