"""JSON CLI; no generic HTTP or arbitrary write commands."""
import argparse
import json
import sys
from .client import Client, READ_ROLES
from .common import Error, canonical, guid, load_json, require
from .importer import Importer
from .state import State
from .transport import Config, Transport


def parser():
    result = argparse.ArgumentParser(description="1С:Бухгалтерия 3.0 — OData, только создание без проведения")
    result.add_argument("--profile", help="Проверенный JSON-профиль; по умолчанию profile.json в каталоге состояния базы")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("discover")
    listing = commands.add_parser("list")
    listing.add_argument("role", choices=sorted(READ_ROLES))
    listing.add_argument("--where", default="{}", help="JSON точных условий по логическим полям")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--all", action="store_true")
    find = commands.add_parser("find-counterparty")
    find.add_argument("--inn", required=True)
    find.add_argument("--kpp", default="")
    imp = commands.add_parser("import")
    imp.add_argument("input", help="Путь к нормализованному JSON")
    imp.add_argument("--dry-run", action="store_true")
    status = commands.add_parser("operation-status")
    status.add_argument("operation_id")
    return result


def run(args):
    config = Config()
    state = State(config.state_dir, config.base)
    if args.command == "operation-status":
        return {"ok": True, "operation": state.read(guid(args.operation_id))}
    transport = Transport(config, log=state.log)
    if args.command in {"check", "discover"}:
        client = Client(transport)
        if args.command == "discover":
            state.save(state.directory / "metadata.json", client.schema.data)
            template = {"version": 1, "reviewed": False, "metadata_sha256": client.schema.data["sha256"],
                        "vat_rates": {}, "roles": {role: {"entity_set": "", "fields": {}, "defaults": {}, "required_on_create": []}
                                                  for role in sorted(READ_ROLES - {"currencies"})}}
            state.save(state.directory / "profile-template.json", template)
            return {"ok": True, "metadata": client.schema.data,
                    "profile_path": str(state.profile_path), "template_path": str(state.directory / "profile-template.json"), "write_access": "not_tested"}
        return {"ok": True, "metadata_sha256": client.schema.data["sha256"],
                "entity_set_count": len(client.schema.data["entity_sets"]), "write_access": "not_tested"}
    profile = load_json(args.profile or state.profile_path)
    client = Client(transport, profile=profile)
    if args.command == "list":
        try:
            where = json.loads(args.where)
        except (ValueError, UnicodeError):
            raise Error("invalid_input", "--where должен содержать JSON-объект.") from None
        require(isinstance(where, dict) and 1 <= args.limit <= 10000, "Проверьте --where и --limit.")
        return {"ok": True, "items": client.query(args.role, where, None if args.all else args.limit)}
    if args.command == "find-counterparty":
        from .common import tax_id
        inn, kpp = tax_id({"inn": args.inn, "kpp": args.kpp})
        rows = client.query("counterparties", {"inn": inn, "kpp": kpp})
        return {"ok": True, "items": rows, "ambiguous": len(rows) > 1}
    if args.command == "import":
        return Importer(client, state).execute(load_json(args.input), args.dry_run)
    raise Error("invalid_input", "Неизвестная команда.")


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
        print(canonical({"ok": False, "error": "interrupted", "message": "Операция прервана. Перед повтором проверьте operation-status."}))
        return 130
    except Exception:
        # Never print exception messages or tracebacks containing credentials or source data.
        print(canonical({"ok": False, "error": "internal_error", "message": "Внутренняя ошибка. Проверьте журнал и состояние операции перед повтором."}))
        return 3
