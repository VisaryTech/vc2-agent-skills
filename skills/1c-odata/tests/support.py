"""Synthetic CSDL, deliberately unrelated to actual 1C entity names."""
import copy
import re
import urllib.parse as url
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from onec_lib.common import Error
from onec_lib.metadata import parse_metadata

IDS = {key: str(uuid.UUID(int=i)) for i, key in enumerate(
    ["organization", "supplier", "contract", "item", "unit", "currency", "basis", "operation"], 1)}


def fixture():
    roles = {
        "organizations": {"id": "Edm.Guid", "code": "Edm.String", "inn": "Edm.String", "kpp": "Edm.String"},
        "counterparties": {"id": "Edm.Guid", "inn": "Edm.String", "kpp": "Edm.String", "name": "Edm.String"},
        "contracts": {"id": "Edm.Guid", "code": "Edm.String", "organization": "Edm.Guid", "supplier": "Edm.Guid"},
        "items": {"id": "Edm.Guid", "code": "Edm.String"},
        "units": {"id": "Edm.Guid", "code": "Edm.String"},
        "currencies": {"id": "Edm.Guid", "code": "Edm.String"},
        "receipts": {"id": "Edm.Guid", "organization": "Edm.Guid", "supplier": "Edm.Guid", "contract": "Edm.Guid",
                     "currency": "Edm.Guid", "number": "Edm.String", "date": "Edm.DateTime", "total": "Edm.Decimal",
                     "vat_total": "Edm.Decimal", "posted": "Edm.Boolean", "includes_vat": "Edm.Boolean", "deletion_mark": "Edm.Boolean"},
        "invoices": {"id": "Edm.Guid", "organization": "Edm.Guid", "supplier": "Edm.Guid", "basis": "Edm.Guid",
                     "number": "Edm.String", "date": "Edm.DateTime", "total": "Edm.Decimal", "vat_total": "Edm.Decimal", "posted": "Edm.Boolean"},
    }
    root = ET.Element("Schema", Namespace="Synthetic")
    container = ET.SubElement(root, "EntityContainer", Name="Container")
    profile = {"version": 1, "reviewed": True, "roles": {}, "vat_rates": {"none": "NoTax", "0": "Zero", "20": "Twenty", "22": "TwentyTwo"}}
    for n, (role, props) in enumerate(roles.items()):
        entity = ET.SubElement(root, "EntityType", Name="Type" + str(n))
        key = ET.SubElement(entity, "Key")
        ET.SubElement(key, "PropertyRef", Name="F_id")
        for field, kind in props.items():
            ET.SubElement(entity, "Property", Name="F_" + field, Type=kind)
        ET.SubElement(container, "EntitySet", Name="Set" + str(n), EntityType="Synthetic.Type" + str(n))
        profile["roles"][role] = {"entity_set": "Set" + str(n), "fields": {k: "F_" + k for k in props}}
        if role == "receipts":
            ET.SubElement(entity, "Property", Name="Goods", Type="Collection(Synthetic.Row)")
            ET.SubElement(entity, "Property", Name="Services", Type="Collection(Synthetic.Row)")
            profile["roles"][role]["tables"] = {
                kind: {"property": prop, "amount_mode": "input", "fields": {k: "R_" + k for k in ["item", "unit", "quantity", "price", "amount", "vat", "vat_rate", "line_no"]}}
                for kind, prop in [("goods", "Goods"), ("services", "Services")]}
    row = ET.SubElement(root, "ComplexType", Name="Row")
    for field in ["item", "unit", "quantity", "price", "amount", "vat", "vat_rate", "line_no"]:
        kind = "Edm.Guid" if field in {"item", "unit"} else "Edm.String" if field == "vat_rate" else "Edm.Int32" if field == "line_no" else "Edm.Decimal"
        ET.SubElement(row, "Property", Name="R_" + field, Type=kind)
    raw = ET.tostring(root)
    metadata = parse_metadata(raw)
    profile["metadata_sha256"] = metadata["sha256"]
    return raw, metadata, profile


def document(kind="upd"):
    data = {"operation_id": IDS["operation"], "type": kind, "number": "TEST-ONEC-001", "date": "2026-10-21",
            "organization": {"code": "ORG"}, "supplier": {"inn": "1234567890", "kpp": "123456789", "name": "TEST supplier"},
            "contract": {"code": "CONTRACT"}, "currency": {"code": "RUB"}, "includes_vat": True,
            "total": "120.00", "vat_total": "20.00", "lines": [
                {"kind": "services" if kind == "act" else "goods", "item": {"code": "ITEM"}, "unit": {"code": "UNIT"},
                 "quantity": "2", "price": "60.00", "net": "100.00", "vat": "20.00", "gross": "120.00", "vat_rate": "20"}]}
    if kind == "upd":
        data["function"] = "invoice_transfer"
        data["invoice"] = {"number": "TEST-SF-001", "date": data["date"]}
    if kind == "invoice":
        for field in ["lines", "includes_vat"]:
            data.pop(field)
        data["basis"] = {"number": "TEST-ONEC-001", "date": data["date"]}
        data["number"] = "TEST-SF-002"
    return data


class Service:
    base = "https://example.invalid/base/odata/"

    def __init__(self):
        self.raw, self.metadata, self.profile = fixture()
        self.calls = []
        self.rows = {s["entity_set"]: [] for s in self.profile["roles"].values()}
        seeds = {
            "organizations": {"id": IDS["organization"], "code": "ORG", "inn": "1111111111", "kpp": "111111111"},
            "counterparties": {"id": IDS["supplier"], "inn": "1234567890", "kpp": "123456789", "name": "TEST supplier"},
            "contracts": {"id": IDS["contract"], "code": "CONTRACT", "organization": IDS["organization"], "supplier": IDS["supplier"]},
            "items": {"id": IDS["item"], "code": "ITEM"}, "units": {"id": IDS["unit"], "code": "UNIT"},
            "currencies": {"id": IDS["currency"], "code": "RUB"}}
        for role, values in seeds.items():
            self.rows[self.profile["roles"][role]["entity_set"]].append({"F_" + k: v for k, v in values.items()})
        self.fail_role = None
        self.lose_reply = False
        self.invisible_post = False
        self.fail_read = False

    def safe_url(self, address):
        return url.urljoin(self.base, address)

    def request(self, method, address, payload=None, metadata=False):
        self.calls.append((method, address, copy.deepcopy(payload)))
        if metadata:
            return self.raw
        parts = url.urlsplit(address)
        path = url.unquote(parts.path).rsplit("/", 1)[-1]
        role = next((r for r, s in self.profile["roles"].items() if s["entity_set"] == path.split("(")[0]), None)
        if method == "POST":
            if self.fail_role == role:
                raise Error("http_error", "Rejected", http_status=403)
            row = {"F_id": str(uuid.uuid4()), **copy.deepcopy(payload)}
            if not self.invisible_post:
                self.rows[path].append(row)
            if self.lose_reply or self.invisible_post:
                raise Error("write_outcome_unknown", "Reply lost")
            return row
        if "(guid'" in path:
            if self.fail_read:
                raise Error("temporary_error", "Read unavailable")
            key = path.split("(guid'")[1].split("'")[0]
            return copy.deepcopy(next(r for r in self.rows[path.split("(")[0]] if r["F_id"] == key))
        rows = copy.deepcopy(self.rows[path])
        condition = url.parse_qs(parts.query).get("$filter", [""])[0]
        if condition:
            for field, expr in re.findall(r"(\w+) eq ((?:guid|datetimeoffset|datetime)?'(?:[^']|'')*'|true|false|[\d.]+)(?: and |$)", condition):
                if expr.startswith("guid'"):
                    val = expr[5:-1]
                elif expr.startswith("datetime"):
                    val = expr[expr.index("'") + 1:-1]
                elif expr.startswith("'"):
                    val = expr[1:-1].replace("''", "'")
                else:
                    val = expr
                rows = [r for r in rows if r.get(field) == val]
        return {"value": rows}
