"""Typed JSON filter trees: OData v3 rendering and independent row verification."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import re

from .common import Error, require


NUMERIC = {"Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64",
           "Edm.Decimal", "Edm.Double", "Edm.Single"}
TEMPORAL = {"Edm.DateTime", "Edm.DateTimeOffset"}


class Predicate:
    def __init__(self, client, spec, tree=None, where=None):
        self.client, self.spec, self.fields = client, spec, set()
        self.nodes = 0
        require(where is None or isinstance(where, dict), "Условия должны быть объектом.")
        if where:
            client.encode(spec, where)  # Preserve validation of the existing exact-match interface.
        nodes = [{"op": "eq", "field": name, "value": value} for name, value in (where or {}).items()]
        if tree is not None:
            nodes.append(tree)
        self.root = self.compile({"op": "and", "args": nodes}, 0, allow_empty=True)
        self.text = self.root[0]

    def compile(self, node, depth, allow_empty=False):
        self.nodes += 1
        require(depth <= 32 and self.nodes <= 1000, "Слишком сложный фильтр.")
        require(isinstance(node, dict) and isinstance(node.get("op"), str), "Некорректный узел фильтра.")
        op = node["op"]
        if op in {"and", "or", "not"}:
            require(set(node) == {"op", "args"} and isinstance(node["args"], list), "Ожидались op и args.")
            args = node["args"]
            require(len(args) == 1 if op == "not" else bool(args) or allow_empty, "Неверное число аргументов.")
            children = [self.compile(arg, depth + 1) for arg in args]
            text = "not (" + children[0][0] + ")" if op == "not" else (" " + op + " ").join(
                "(" + child[0] + ")" for child in children)
            return text, op, children
        require(op in {"eq", "ne", "gt", "ge", "lt", "le", "startswith", "endswith", "contains"},
                "Неизвестная операция фильтра.")
        require(set(node) == {"op", "field", "value"}, "Ожидались op, field и value.")
        field = node["field"]
        prop = self.client.schema.property(self.spec, field)
        kind, value = prop["Type"], node["value"]
        require(kind in NUMERIC | TEMPORAL | {"Edm.String", "Edm.Boolean", "Edm.Guid", "Edm.Binary", "Edm.Time"},
                "Тип не поддерживается в фильтре.", "unsupported_type")
        if op in {"startswith", "endswith", "contains"}:
            require(kind == "Edm.String" and isinstance(value, str), "Строковая операция требует строку.")
        elif op not in {"eq", "ne"}:
            require(kind in NUMERIC | TEMPORAL | {"Edm.String"} and value is not None,
                    "Сравнение недопустимо для типа или null.")
        # Null is a valid filter literal even for non-nullable properties.
        literal = "null" if value is None else self.client.literal(prop, value)
        encoded = None if value is None else self.client.value(prop, value)
        self.fields.add(field)
        if op == "contains":
            text = f"substringof({literal},{field})"
        elif op in {"startswith", "endswith"}:
            text = f"{op}({field},{literal})"
        else:
            text = f"{field} {op} {literal}"
        return text, op, field, prop, encoded

    def comparable(self, prop, value):
        kind = prop["Type"]
        if kind in TEMPORAL and isinstance(value, str) and value.startswith("/Date("):
            match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", value)
            require(match is not None, "Некорректная дата ответа.", "invalid_response")
            parsed = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(milliseconds=int(match[1]))
            value = (parsed.replace(tzinfo=None) if kind == "Edm.DateTime" else parsed).isoformat()
        encoded = self.client.value(prop, value)
        if kind in NUMERIC:
            return Decimal(str(encoded))
        if kind in TEMPORAL:
            return dt.datetime.fromisoformat(encoded)
        return encoded

    def evaluate(self, node, row):
        _, op, *args = node
        if op in {"and", "or", "not"}:
            values = [self.evaluate(child, row) for child in args[0]]
            return not values[0] if op == "not" else all(values) if op == "and" else any(values)
        field, prop, expected = args
        actual = row[field]
        if op in {"eq", "ne"}:
            equal = self.client.equal(prop, actual, expected)
            return equal if op == "eq" else not equal
        if actual is None:
            return False
        actual, expected = self.comparable(prop, actual), self.comparable(prop, expected)
        if op == "startswith":
            return actual.startswith(expected)
        if op == "endswith":
            return actual.endswith(expected)
        if op == "contains":
            return expected in actual
        return {"gt": lambda: actual > expected, "ge": lambda: actual >= expected,
                "lt": lambda: actual < expected, "le": lambda: actual <= expected}[op]()

    def matches(self, row):
        require(isinstance(row, dict) and self.fields <= row.keys(),
                "В ответе отсутствуют поля фильтра.", "invalid_response")
        try:
            return self.evaluate(self.root, row)
        except (Error, ValueError, TypeError, InvalidOperation, OverflowError):
            raise Error("invalid_response", "Некорректное значение поля фильтра в ответе.") from None
