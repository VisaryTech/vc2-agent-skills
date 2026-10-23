"""CSDL discovery. Entity names are obtained exclusively from the service."""
import hashlib
import re
import xml.etree.ElementTree as ET
from .common import Error, require


def local(tag):
    return tag.rsplit("}", 1)[-1]


def annotations(elem):
    """Preserve inline CSDL annotations without interpreting business semantics."""
    def node(value):
        return {"kind": local(value.tag), **dict(value.attrib), "text": (value.text or "").strip(),
                "children": [node(child) for child in value]}
    return [node(child) for child in elem if local(child.tag) == "Annotation"]


def parse_metadata(raw):
    require(b"<!DOCTYPE" not in raw.replace(b"\x00", b"").upper() and b"<!ENTITY" not in raw.replace(b"\x00", b"").upper(),
            "DTD и XML entities запрещены.", "invalid_metadata")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise Error("invalid_metadata", "Некорректный XML metadata.") from None
    types, sets, operations, aliases, targets = {}, {}, [], {}, {}
    schemas = [e for e in root.iter() if local(e.tag) == "Schema"]
    for schema in schemas:
        namespace = schema.get("Namespace", "")
        if schema.get("Alias"):
            aliases[schema.get("Alias")] = namespace
        for elem in schema:
            kind = local(elem.tag)
            if kind in {"EntityType", "ComplexType", "EnumType"}:
                name = namespace + "." + elem.get("Name", "")
                properties, navigation, keys, members = {}, {}, [], []
                for child in elem:
                    tag = local(child.tag)
                    if tag == "Property":
                        properties[child.get("Name")] = dict(child.attrib)
                    elif tag == "NavigationProperty":
                        navigation[child.get("Name")] = dict(child.attrib)
                    elif tag == "Key":
                        keys = [p.get("Name") for p in child if local(p.tag) == "PropertyRef"]
                    elif tag == "Member":
                        members.append(child.get("Name"))
                types[name] = {"kind": kind, "base": elem.get("BaseType"), "properties": properties,
                               "navigation": navigation, "keys": keys, "members": members,
                               "annotations": annotations(elem)}
                targets[name] = types[name]
            elif kind == "EntityContainer":
                for child in elem:
                    if local(child.tag) == "EntitySet":
                        name = child.get("Name")
                        require(name not in sets, "Неоднозначные EntitySet.", "invalid_metadata")
                        sets[name] = {"type": child.get("EntityType"), "attributes": dict(child.attrib),
                                      "annotations": annotations(child)}
                        targets[namespace + "." + elem.get("Name", "") + "/" + name] = sets[name]
                    elif local(child.tag) in {"FunctionImport", "ActionImport"}:
                        operations.append(dict(child.attrib))
            elif kind in {"Function", "Action", "Association"}:
                operations.append({"kind": kind, **dict(elem.attrib),
                                   "children": [dict(c.attrib) for c in elem]})
    def qualify(name):
        if name and name.startswith("Collection("):
            return "Collection(" + qualify(name[11:-1]) + ")"
        if name and "." in name:
            head, tail = name.split(".", 1)
            return aliases.get(head, head) + "." + tail
        return name
    for spec in types.values():
        spec["base"] = qualify(spec["base"])
        for prop in spec["properties"].values():
            prop["Type"] = qualify(prop.get("Type"))
    for spec in sets.values():
        spec["type"] = qualify(spec["type"])
    for elem in root.iter():
        if local(elem.tag) == "Annotations":
            target = targets.get(qualify(elem.get("Target")))
            if target is not None:
                target["annotations"].extend(annotations(elem))
    require(bool(sets), "Metadata не содержит EntitySet.", "invalid_metadata")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "types": types,
            "entity_sets": sets, "operations": operations}


class Schema:
    def __init__(self, data):
        self.data = data

    def type(self, name, seen=()):
        require(name in self.data["types"] and name not in seen, "Неизвестный или циклический тип.", "invalid_metadata")
        result = dict(self.data["types"][name])
        if result["base"]:
            parent = self.type(result["base"], (*seen, name))
            result["properties"] = {**parent["properties"], **result["properties"]}
            result["keys"] = result["keys"] or parent["keys"]
            result["navigation"] = {**parent["navigation"], **result["navigation"]}
        return result

    def entity(self, name):
        require(isinstance(name, str) and bool(re.fullmatch(r"[^\W\d]\w*", name))
                and name in self.data["entity_sets"], "EntitySet отсутствует в metadata.", "invalid_input")
        result = self.type(self.data["entity_sets"][name]["type"])
        require(result["kind"] == "EntityType", "Ожидался EntityType.", "invalid_input")
        return result

    def property(self, spec, name):
        require(isinstance(name, str) and name in spec["properties"], "Поле отсутствует в metadata.", "invalid_input")
        return spec["properties"][name]
