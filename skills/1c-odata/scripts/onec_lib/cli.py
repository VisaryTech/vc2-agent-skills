"""JSON CLI for metadata discovery, reads and single-object CRUD."""
import argparse
import json
from .common import Error, WireNumber, canonical, load_json, require
from .generic import EntityClient, Mutations
from .transport import Config, Transport


def parser():
    result = argparse.ArgumentParser(description="1С OData — универсальный доступ по метаданным OData")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("discover")
    entities = commands.add_parser("entities")
    entities.add_argument("--search", default="")
    describe = commands.add_parser("describe")
    describe.add_argument("entity")
    query = commands.add_parser("query")
    query.add_argument("entity")
    query.add_argument("--where", default="{}", help="JSON точных условий по физическим полям")
    query.add_argument("--select", default="", help="Поля через запятую")
    query.add_argument("--order-by", default="", help="Например: Date desc,Number asc")
    query.add_argument("--limit", type=int, default=100)
    query.add_argument("--all", action="store_true")
    get = commands.add_parser("get")
    get.add_argument("entity")
    get.add_argument("--key", required=True, help="JSON-объект ключевых полей")
    for name in ("create", "update", "mark-deletion", "delete"):
        command = commands.add_parser(name)
        command.add_argument("entity")
        if name in {"create", "update"}:
            command.add_argument("input", help="JSON-файл с физическими полями")
        if name != "create":
            command.add_argument("--key", required=True, help="JSON-объект ключевых полей")
        if name == "mark-deletion":
            command.add_argument("--value", choices=("true", "false"), default="true")
        command.add_argument("--dry-run", action="store_true")
    return result


def run(args):
    config = Config()
    transport = Transport(config, allow_mutations=args.command in {
        "create", "update", "mark-deletion", "delete"} and not args.dry_run)
    client = EntityClient(transport)
    if args.command in {"check", "discover"}:
        result = {"ok": True, "metadata_sha256": client.schema.data["sha256"],
                  "entity_set_count": len(client.schema.data["entity_sets"]), "write_access": "not_tested"}
        if args.command == "discover":
            result["metadata"] = client.schema.data
        return result
    if args.command == "entities":
        return {"ok": True, "items": client.entities(args.search)}
    if args.command == "describe":
        return {"ok": True, "schema": client.describe(args.entity)}
    if args.command == "query":
        require(1 <= args.limit <= 10000, "Лимит должен быть 1–10000.")
        return {"ok": True, "items": client.query_entity(args.entity, json_object(args.where),
                [name.strip() for name in args.select.split(",") if name.strip()],
                [term.strip() for term in args.order_by.split(",") if term.strip()],
                None if args.all else args.limit)}
    key = json_object(args.key) if hasattr(args, "key") else None
    if args.command == "get":
        return {"ok": True, "item": client.get(args.entity, key)}
    data = load_json(args.input, exact=True) if hasattr(args, "input") else None
    if args.command == "mark-deletion":
        data = {"DeletionMark": args.value == "true"}
    return Mutations(client).execute(args.entity, args.command, data, key, args.dry_run)


def json_object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Повторяющийся ключ JSON.")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_float=WireNumber,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise Error("invalid_input", "Требуется корректный JSON-объект.") from None
    require(isinstance(value, dict), "Требуется JSON-объект.")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(canonical(result))
        return 0
    except Error as exc:
        print(canonical(exc.result()))
        return 2
    except KeyboardInterrupt:
        print(canonical({"ok": False, "error": "interrupted", "message": "Операция прервана. Если выполнялась запись, перед повтором проверьте результат в 1С."}))
        return 130
    except Exception:
        # Never print exception messages or tracebacks containing credentials or source data.
        print(canonical({"ok": False, "error": "internal_error", "message": "Внутренняя ошибка. Если выполнялась запись, перед повтором проверьте результат в 1С."}))
        return 3
