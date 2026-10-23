"""Command line interface with structured errors and token redaction."""
import argparse
import json
import math
from pathlib import Path
import sys

from .auth import TokenProvider, load_credentials, validate_env
from .client import Client
from .common import MAX_BYTES, ClientError, guid, redact, require
from .drafts import copy_draft, prepare
from .transport import HttpTransport


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse can echo arbitrary supplied arguments, including secrets.
        raise ClientError("invalid_arguments", "Неверные аргументы; используйте --help.")


def reject_constant(value):
    raise ValueError("JSON constant is not supported")


def load_json(path):
    try:
        if path == "-":
            data = sys.stdin.read(MAX_BYTES + 1)
        else:
            with Path(path).open(encoding="utf-8") as stream:
                data = stream.read(MAX_BYTES + 1)
        require(len(data.encode("utf-8")) <= MAX_BYTES, "JSON превышает лимит 100 МиБ.")
        value = json.loads(data, parse_constant=reject_constant)
        require(isinstance(value, dict), "Ожидался JSON-объект.")
        return value
    except (OSError, UnicodeError, ValueError):
        raise ClientError("invalid_input", "Не удалось прочитать JSON-объект.") from None


def parser():
    p = Parser(description="Диадок: документы и черновики через прямой API")
    p.add_argument("--env-file")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--box-id", help="GUID ящика или BoxId с суффиксом @diadoc.ru")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check-env", help="проверить настройки без подключения к API")
    sub.add_parser("check")
    sub.add_parser("organizations")
    listing = sub.add_parser("list")
    listing.add_argument("--filter", required=True)
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--max-pages", type=int, default=10000)
    for command in ("read", "download"):
        read = sub.add_parser(command)
        read.add_argument("message_id")
        read.add_argument("--entity-id")
        if command == "download":
            read.add_argument("--output", required=True)
    for command in ("draft-create", "draft-copy"):
        draft = sub.add_parser(command)
        if command == "draft-copy":
            draft.add_argument("message_id")
        draft.add_argument("--input", required=True)
        draft.add_argument("--dry-run", action="store_true")
        draft.add_argument("--operation-id", required=True, help="UUID; повторять только с неизменным запросом")
    return p


def run(args, client):
    if args.command == "check":
        client.organizations()
        return {"ok": True}
    if args.command == "organizations":
        return client.organizations()
    client.box()
    if args.command == "list":
        return client.documents(load_json(args.filter), args.all, args.max_pages)
    if args.command == "read":
        return client.read(args.message_id, args.entity_id)
    if args.command == "download":
        return client.download(args.message_id, args.output, args.entity_id)
    operation_id = guid(args.operation_id)
    value = load_json(args.input)
    payload = (prepare(value, client.box()) if args.command == "draft-create"
               else copy_draft(client, args.message_id, value))
    if args.dry_run:
        return {"dry_run": True, "operation_id": operation_id, "request": redact(payload, preview=True)}
    result = client.post_draft(payload, operation_id)
    if args.command == "draft-copy":
        return {"source_message_id": args.message_id, "new_message_id": result["MessageId"],
                "operation_id": operation_id, "Message": result}
    return {"operation_id": operation_id, "Message": result}


def main(argv=None):
    secrets = []
    args = None
    try:
        args = parser().parse_args(argv)
        if args.command == "check-env":
            print(json.dumps(validate_env(env_file=args.env_file), ensure_ascii=False))
            return 0
        require(math.isfinite(args.timeout) and args.timeout > 0, "timeout должен быть конечным положительным числом.")
        transport = HttpTransport(args.timeout)
        provider = TokenProvider(load_credentials(env_file=args.env_file), transport)
        secrets = provider.secrets
        client = Client(box_id=args.box_id, transport=transport, token_provider=provider)
        result = run(args, client)
        print(json.dumps(redact(result, secrets), ensure_ascii=False, allow_nan=False))
        return 0
    except ClientError as exc:
        if args is not None and args.command == "check-env":
            print(json.dumps({"ok": False, "error": "configuration_error", "message": str(exc)},
                             ensure_ascii=False))
            return 2
        error = {"code": exc.code, "message": str(exc), **exc.details}
    except (OSError, ValueError, TypeError, KeyError):
        error = {"code": "client_error", "message": "Не удалось обработать данные или файлы; проверьте входные данные и доступ."}
    print(json.dumps(redact({"error": error}, secrets), ensure_ascii=False), file=sys.stderr)
    return 1
