"""Strict input primitives and structured, non-sensitive errors."""
from decimal import Decimal
import json
import re

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


def load_json(path, exact=False):
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
        return json.loads(raw, object_pairs_hook=unique, parse_float=WireNumber if exact else float,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (OSError, ValueError, UnicodeError):
        raise Error("file_error", "Не удалось прочитать корректный JSON.") from None


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
