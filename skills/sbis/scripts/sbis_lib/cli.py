"""Command-line arguments, dispatch and JSON diagnostics."""

import argparse
import json
import math
import sys

from .auth import validate_env
from .client import Client
from .common import DEFAULT_MAX_PAGES, HOSTS, MAX_REQUEST, ClientError, format_size, redact, require
from .drafts import prepare_draft, require_unlocked
from .transport import HttpTransport


def load_json(path):
    try:
        if path == "-":
            data = json.load(sys.stdin)
        else:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, ValueError):
        raise ClientError("invalid_input", "Не удалось прочитать JSON из файла или stdin.") from None
    require(isinstance(data, dict), "Ожидался JSON-объект.")
    return data


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ClientError("invalid_arguments", "Неверные аргументы. Используйте --help.")


def parser():
    p = Parser(description="СБИС ЭДО: документы и черновики (Python 3.10+).")
    p.add_argument("--profile", choices=HOSTS, default="production")
    p.add_argument("--env-file", help="путь к .env вместо файла в текущем рабочем каталоге")
    p.add_argument("--cache-dir", help="каталог приватного кэша токена вместо стандартного")
    p.add_argument("--timeout", type=float, default=60, help="таймаут HTTPS в секундах")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    sub.add_parser("check-env", help="проверить настройки без подключения к API")
    sub.add_parser("check", help="проверить доступ к организациям ЭДО")
    sub.add_parser("organizations", help="получить организации аккаунта")
    ls = sub.add_parser("list", help="список документов")
    ls.add_argument("--filter", required=True, help="JSON-файл с объектом Фильтр (без обёртки), или -")
    ls.add_argument("--all", action="store_true", help="явно обойти все страницы")
    ls.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                    help=f"максимум запросов страниц с --all (по умолчанию {DEFAULT_MAX_PAGES})")
    read = sub.add_parser("read", help="документ, вложения и текущее состояние")
    read.add_argument("id")
    dl = sub.add_parser("download", help="скачать вложения и подписи без распаковки")
    dl.add_argument("id")
    dl.add_argument("--output", required=True, help="каталог выгрузки")
    dl.add_argument("--archive", action="store_true", help="скачать ZIP по Документ.СсылкаНаАрхив")
    for name in ("draft-create", "draft-update"):
        draft = sub.add_parser(name, help="создать/обновить исходящий черновик")
        if name == "draft-update":
            draft.add_argument("id")
            draft.add_argument("--replace-attachment", action="append", default=[], metavar="ID=PATH")
            draft.add_argument("--remove-attachment", action="append", default=[], metavar="ID")
        draft.add_argument("--input", required=True, help="JSON-объект Документ без обёртки, или -")
        draft.add_argument("--attachment", action="append", default=[], metavar="PATH")
        draft.add_argument("--dry-run", action="store_true", help="подготовить запрос без записи")
    return p


def run(args, client):
    if args.command == "check":
        client.rpc("СБИС.СписокНашихОрганизаций", {"Фильтр": {}})
        return {"ok": True, "profile": client.profile}
    if args.command == "organizations":
        return client.rpc("СБИС.СписокНашихОрганизаций", {"Фильтр": {}})
    if args.command == "list":
        return client.list_documents(load_json(args.filter), args.all, args.max_pages)
    if args.command == "read":
        return client.read(args.id)
    if args.command == "download":
        return client.download(args.id, args.output, archive=args.archive)
    document = prepare_draft(client, load_json(args.input), getattr(args, "id", None),
                             args.attachment, getattr(args, "replace_attachment", []),
                             getattr(args, "remove_attachment", []))
    params = {"Документ": document}
    # Apply the same size limit to previews, including the JSON-RPC envelope.
    envelope = {"jsonrpc": "2.0", "id": client.counter + 1,
                "method": "СБИС.ЗаписатьДокумент", "params": params}
    require(len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) <= MAX_REQUEST,
            f"Размер JSON-запроса превышает {format_size(MAX_REQUEST)}.")
    if args.dry_run:
        return {"dry_run": True, "method": "СБИС.ЗаписатьДокумент",
                "params": redact(params, preview=True)}
    if args.command == "draft-update":
        # Downloads can take time: refuse an intervening edit or state change.
        current = client.read(args.id, include_extension=True)
        require_unlocked(current)
        require(current == client.draft_snapshot,
                "Документ изменился во время подготовки. Прочитайте его заново и повторите предпросмотр.",
                "document_changed")
    return client.rpc("СБИС.ЗаписатьДокумент", params, write=True)


def main(argv=None):
    client = None
    args = None
    try:
        args = parser().parse_args(argv)
        if args.command == "check-env":
            print(json.dumps(validate_env(env_file=args.env_file), ensure_ascii=False))
            return 0
        require(math.isfinite(args.timeout) and args.timeout > 0, "Таймаут должен быть положительным числом.")
        client = Client(args.profile, HttpTransport(args.timeout), env_file=args.env_file,
                        cache_root=args.cache_dir)
        result = run(args, client)
        print(json.dumps(redact(result, client.secrets), ensure_ascii=False))
        return 0
    except ClientError as exc:
        if args is not None and args.command == "check-env":
            print(json.dumps({"ok": False, "error": "configuration_error", "message": str(exc)},
                             ensure_ascii=False))
            return 2
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print(json.dumps({"error": {"code": "local_error", "message": "Ошибка локальных данных или файлов; подробности скрыты."}}, ensure_ascii=False), file=sys.stderr)
        return 1
