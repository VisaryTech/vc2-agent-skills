"""Offline behavior checks. Run: python3 -m unittest discover -s skills/sbis/tests -v."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import http.client
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sbis_lib import auth, cli, client as api, common, drafts, transport as http_transport



CREDENTIALS = {"SBIS_APP_CLIENT_ID": "application-id", "SBIS_APP_SECRET": "app-secret",
               "SBIS_SECRET_KEY": "service-key"}


class FakeHttp:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.downloads = []

    def post(self, url, payload, headers):
        self.calls.append((url, copy.deepcopy(payload), headers.copy()))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):
            return result
        if url.endswith("/oauth/service/"):
            return 200, json.dumps(result).encode()
        envelope = {"jsonrpc": "2.0", "id": payload["id"]}
        if isinstance(result, dict) and "error" in result:
            envelope.update(result)
        else:
            envelope["result"] = result
        return 200, json.dumps(envelope, ensure_ascii=False).encode()

    def get(self, url):
        self.downloads.append(url)
        return b"\x00\xffbinary"


def client(*results, **kwargs):
    c = api.Client(transport=FakeHttp(*results), env=CREDENTIALS, **kwargs)
    c.token = "private-token"
    c.secrets.append(c.token)
    return c


def draft():
    return {"Идентификатор": "doc", "Тип": "ДокОтгрИсх", "Направление": "Исходящий",
            "Состояние": {"Код": "0", "Название": "Документ редактируется"},
            "Расширение": {"ЗакрытОтИзменений": "Нет"},
            "Редакция": {"Идентификатор": "revision"}, "Номер": "42",
            "НашаОрганизация": {"СвЮЛ": {"ИНН": "1234567890"}},
            "Вложение": [{"Идентификатор": "a1", "Файл": {
                "Имя": "акт.pdf", "Ссылка": "https://disk.sbis.ru/attachment"},
                "Подпись": {"Файл": {"Имя": "акт.pdf.sgn", "Ссылка": "https://disk.sbis.ru/signature"}}}]}


class TransportTests(unittest.TestCase):
    def test_rpc_unicode_token_and_profiles(self):
        c = client({"Имя": "Иван"}, profile="test")
        self.assertEqual(c.rpc("test", {"Название": "Счёт"}), {"Имя": "Иван"})
        url, body, headers = c.transport.calls[0]
        self.assertEqual(url, "https://fix-online.sbis.ru/service/?srv=1")
        self.assertEqual(body["params"]["Название"], "Счёт")
        self.assertEqual(headers["X-SBISAccessToken"], "private-token")

    def test_http_200_rpc_error_does_not_echo_server_secrets(self):
        c = client({"error": {"code": -32000, "message": "password=unsafe", "data": {"sid": "secret"}}})
        with self.assertRaises(common.ClientError) as ctx:
            c.rpc("test", {})
        self.assertEqual(ctx.exception.code, "rpc_error")
        self.assertNotIn("unsafe", str(ctx.exception))
        self.assertNotIn("secret", str(ctx.exception))

    def test_rpc_error_has_no_interactive_login_or_retry(self):
        c = client({"error": {"data": {"classid": "{00000000-0000-0000-0000-1fa000001002}"}}})
        with self.assertRaises(common.ClientError) as ctx:
            c.rpc("test", {})
        self.assertEqual(ctx.exception.code, "rpc_error")
        self.assertEqual(len(c.transport.calls), 1)

    def test_http_errors(self):
        for status, code in [(401, "token_expired"), (403, "access_denied"), (429, "http_error")]:
            with self.subTest(status=status):
                c = client(urllib.error.HTTPError("https://hidden", status, "secret", {}, None))
                with self.assertRaises(common.ClientError) as ctx:
                    c.rpc("test", {})
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(len(c.transport.calls), 1)

    def test_http_errors_close_responses_and_keep_diagnostics_private(self):
        for command, status, expected in [
                ("rpc", 401, "token_expired"), ("rpc", 403, "access_denied"),
                ("rpc", 429, "http_error"), ("write", 503, "write_outcome_unknown"),
                ("auth", 403, "access_denied"), ("file", 403, "access_denied")]:
            with self.subTest(command=command, status=status), tempfile.TemporaryDirectory() as directory:
                body = io.BytesIO(b"private response body")
                error = urllib.error.HTTPError("https://disk.sbis.ru/private-link", status,
                                               "private server message", {}, body)
                self.addCleanup(error.close)
                c = client(error, cache_root=directory)
                if command == "auth":
                    c.token = None
                with self.assertRaises(common.ClientError) as ctx:
                    if command == "file":
                        with patch.object(c.transport, "get", side_effect=error):
                            c.get_file({"Ссылка": "https://disk.sbis.ru/private-link"})
                    else:
                        c.rpc("test", {}, write=command == "write")
                self.assertEqual(ctx.exception.code, expected)
                self.assertTrue(body.closed)
                self.assertNotIn("private", str(ctx.exception))
                self.assertEqual(len(c.transport.calls), 0 if command == "file" else 1)

    def test_uncertain_writes_never_retry(self):
        outcomes = [TimeoutError("secret"), urllib.error.URLError("secret"),
                    http.client.IncompleteRead(b"secret", 12),
                    urllib.error.HTTPError("hidden", 503, "secret", {}, None),
                    (503, b"secret"), (200, b"not-json"), (200, b'{}')]
        for result in outcomes:
            with self.subTest(result=result):
                c = client(result)
                with self.assertRaises(common.ClientError) as ctx:
                    c.rpc("СБИС.ЗаписатьДокумент", {}, write=True)
                self.assertEqual(ctx.exception.code, "write_outcome_unknown")
                self.assertEqual(len(c.transport.calls), 1)

    def test_redaction_nested_and_literal(self):
        data = {"sid": "session", "nested": [{"Пароль": "pw", "message": "prefix session"}]}
        self.assertNotIn("session", json.dumps(common.redact(data, ["session"])))
        self.assertNotIn('"pw"', json.dumps(common.redact(data)))

    def test_auth_cache_and_credentials(self):
        for profile in common.HOSTS:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                http = FakeHttp({"token": "cached-token"}, {"ok": True}, None)
                c = api.Client(profile, transport=http, env=CREDENTIALS, cache_root=directory)
                c.rpc("check", {})
                self.assertEqual(http.calls[0][0], common.HOSTS[profile] + "/oauth/service/")
                self.assertEqual(http.calls[0][1], {
                    "app_client_id": "application-id", "app_secret": "app-secret", "secret_key": "service-key"})
                self.assertNotIn("X-SBISAccessToken", http.calls[0][2])
                self.assertNotIn("X-SBISSessionID", http.calls[1][2])
                self.assertEqual(http.calls[1][2]["X-SBISAccessToken"], "cached-token")
                c.rpc("check", {})
                self.assertEqual(len(http.calls), 3)
                self.assertEqual(stat.S_IMODE(c.cache.path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(c.cache.root.stat().st_mode), 0o700)
                second = api.Client(profile, transport=FakeHttp(None), env=CREDENTIALS, cache_root=directory)
                self.assertIsNone(second.rpc("check", {}))
                self.assertEqual(len(second.transport.calls), 1)
                self.assertEqual(second.transport.calls[0][2]["X-SBISAccessToken"], "cached-token")
                expired = api.Client(profile, transport=FakeHttp((401, b"")), env=CREDENTIALS, cache_root=directory)
                with self.assertRaises(common.ClientError) as ctx:
                    expired.rpc("check", {})
                self.assertEqual(ctx.exception.code, "token_expired")
                self.assertIsNone(expired.token)
                self.assertFalse(expired.cache.path.exists())
                self.assertEqual(len(expired.transport.calls), 1)

    def test_cache_separation_permissions_and_old_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = auth.TokenCache("production", CREDENTIALS, directory)
            cache.save("secret")
            self.assertIsNone(auth.TokenCache("test", CREDENTIALS, directory).load())
            for key in auth.ENV_KEYS:
                changed = dict(CREDENTIALS, **{key: "different"})
                self.assertIsNone(auth.TokenCache("production", changed, directory).load())
            cache.path.write_text('{"session":"old-session"}')
            self.assertIsNone(cache.load())
            cache.save("secret")
            cache.path.chmod(0o644)
            self.assertIsNone(cache.load())

    def test_cache_directory_precedence_and_xdg_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            xdg = Path(directory) / "xdg"
            explicit = Path(directory) / "explicit"
            with patch.object(Path, "home", return_value=home):
                for value in ("", "relative/cache"):
                    with patch.dict(os.environ, {"XDG_CACHE_HOME": value}):
                        self.assertEqual(auth.TokenCache("production", CREDENTIALS).root,
                                         home / ".cache" / "sbis")
                with patch.dict(os.environ, {"XDG_CACHE_HOME": str(xdg)}):
                    self.assertEqual(auth.TokenCache("production", CREDENTIALS).root, xdg / "sbis")
                    self.assertEqual(auth.TokenCache("production", CREDENTIALS, explicit).root, explicit)

    def test_cli_passes_explicit_cache_directory(self):
        c = client({})
        with patch.object(cli, "Client", return_value=c) as constructor, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--cache-dir", "/custom/cache", "check"]), 0)
        self.assertEqual(constructor.call_args.kwargs["cache_root"], "/custom/cache")

    def test_auth_errors_are_private_and_never_write_or_retry(self):
        outcomes = [(200, b'not-json'), (200, b'[]'), (200, b'{"token":42}'),
                    (200, b'{"access_token":"secret"}'), (200, b'{"token":""}'),
                    (200, b'{"error":"app-secret"}'), (200, b'{"token":"   "}'),
                    (401, b'app-secret'), (403, b'app-secret'), (503, b'app-secret'),
                    TimeoutError("app-secret"),
                    urllib.error.HTTPError("hidden", 401, "app-secret", {}, None)]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                c = api.Client(transport=FakeHttp(outcome), env=CREDENTIALS, cache_root=directory)
                with self.assertRaises(common.ClientError) as ctx:
                    c.rpc("СБИС.ЗаписатьДокумент", {}, write=True)
                self.assertNotEqual(ctx.exception.code, "write_outcome_unknown")
                self.assertNotIn("app-secret", str(ctx.exception))
                self.assertEqual(len(c.transport.calls), 1)
                self.assertFalse(c.cache.path.exists())

    def test_token_and_credentials_redacted_from_cli_output(self):
        with tempfile.TemporaryDirectory() as directory:
            c = api.Client(transport=FakeHttp({"token": "private-token"}, {
                "message": "app-secret service-key private-token", "SBIS_APP_SECRET": "other"}),
                env=CREDENTIALS, cache_root=directory)
            out = io.StringIO()
            with patch.object(cli, "Client", return_value=c), redirect_stdout(out):
                self.assertEqual(cli.main(["organizations"]), 0)
            for secret in ("app-secret", "service-key", "private-token", "other"):
                self.assertNotIn(secret, out.getvalue())


class CredentialsTests(unittest.TestCase):
    def test_environment_only_does_not_read_default_file(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("unexpected file read")):
            self.assertEqual(auth.load_credentials(CREDENTIALS), CREDENTIALS)

    def test_dotenv_fallback_quotes_comments_and_literal_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "cwd", return_value=Path(directory)):
            Path(directory, ".env").write_text(
                "# comment\nSBIS_APP_CLIENT_ID = 'file id' # trailing\n"
                'SBIS_APP_SECRET="literal ${HOME} $(command) # value" # comment\n'
                "SBIS_SECRET_KEY=key#part # trailing\n", encoding="utf-8")
            expected = {"SBIS_APP_CLIENT_ID": "file id", "SBIS_APP_SECRET": "literal ${HOME} $(command) # value",
                        "SBIS_SECRET_KEY": "key#part"}
            self.assertEqual(auth.load_credentials({}), expected)
            self.assertEqual(auth.load_credentials({"SBIS_APP_CLIENT_ID": "override"}),
                             dict(expected, SBIS_APP_CLIENT_ID="override"))
            self.assertEqual(auth.load_credentials({"SBIS_APP_CLIENT_ID": ""})["SBIS_APP_CLIENT_ID"], "")
            c = api.Client(env={"SBIS_APP_CLIENT_ID": ""}, transport=FakeHttp())
            with self.assertRaises(common.ClientError) as ctx:
                c.authenticate()
            self.assertEqual(ctx.exception.code, "credentials_required")
            self.assertFalse(c.transport.calls)

    def test_cli_loads_working_directory_file_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "SBIS_APP_CLIENT_ID=default-id\nSBIS_APP_SECRET=file-secret\nSBIS_SECRET_KEY=file-key\n")
            explicit = root / "custom.env"
            explicit.write_text(
                "SBIS_APP_CLIENT_ID=custom-id\nSBIS_APP_SECRET=custom-secret\nSBIS_SECRET_KEY=custom-key\n")
            for argv, expected_id, expected_key in [
                    (["check"], "default-id", "file-key"),
                    (["--env-file", str(explicit), "check"], "custom-id", "custom-key")]:
                http = FakeHttp({"token": "fresh-token"}, {})
                out, err = io.StringIO(), io.StringIO()
                with patch.object(Path, "cwd", return_value=root), patch.object(Path, "home", return_value=root), \
                        patch.dict(os.environ, {"SBIS_APP_SECRET": "environment-secret"}, clear=True), \
                        patch.object(cli, "HttpTransport", return_value=http), \
                        redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(cli.main(argv), 0)
                self.assertTrue(json.loads(out.getvalue())["ok"])
                self.assertEqual(err.getvalue(), "")
                self.assertEqual(http.calls[0][1], {"app_client_id": expected_id,
                    "app_secret": "environment-secret", "secret_key": expected_key})

    def test_empty_and_missing_credentials_prevent_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.env"
            for key in auth.ENV_KEYS:
                for suffix in ("", " # empty", "''", '\"\"', "   "):
                    path.write_text("\n".join(f"{k}={suffix if k == key else v}"
                                             for k, v in CREDENTIALS.items()))
                    c = api.Client(env={}, env_file=path, transport=FakeHttp())
                    with self.assertRaises(common.ClientError) as ctx:
                        c.rpc("check", {})
                    self.assertEqual(ctx.exception.code, "credentials_required")
                    self.assertFalse(c.transport.calls)
                path.write_text("\n".join(f"{k}={v}" for k, v in CREDENTIALS.items() if k != key))
                c = api.Client(env={}, env_file=path, transport=FakeHttp())
                with self.assertRaises(common.ClientError) as ctx:
                    c.rpc("check", {})
                self.assertEqual(ctx.exception.code, "credentials_required")
                self.assertFalse(c.transport.calls)

    def test_missing_default_and_invalid_explicit_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "cwd", return_value=Path(directory)):
            self.assertEqual(auth.load_credentials({}), {})
            c = api.Client(env={"SBIS_SESSION_ID": "old", "SBIS_LOGIN": "old", "SBIS_PASSWORD": "old"},
                           transport=FakeHttp())
            with self.assertRaises(common.ClientError) as ctx:
                c.authenticate()
            self.assertEqual(ctx.exception.code, "credentials_required")
            self.assertFalse(c.transport.calls)
            for path in (Path(directory) / "missing", Path(directory)):
                with self.assertRaises(common.ClientError) as ctx:
                    auth.load_credentials(CREDENTIALS, path)
                self.assertEqual(ctx.exception.code, "config_error")
            with patch.object(Path, "read_text", side_effect=PermissionError("secret")):
                with self.assertRaises(common.ClientError) as ctx:
                    auth.load_credentials({})
                self.assertNotIn("secret", str(ctx.exception))

    def test_explicit_file_and_private_parse_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.env"
            path.write_text("SBIS_APP_CLIENT_ID=custom\n")
            self.assertEqual(auth.load_credentials({}, path), {"SBIS_APP_CLIENT_ID": "custom"})
            for content in ('SBIS_APP_SECRET="private', 'SBIS_APP_SECRET="private" junk', 'private value'):
                path.write_text(content)
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err), patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(cli.main(["--env-file", str(path), "check"]), 1)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(json.loads(err.getvalue())["error"]["code"], "config_error")
                self.assertNotIn("private", err.getvalue())


class DocumentTests(unittest.TestCase):
    def test_pagination_budget_counts_requests_from_any_start_page(self):
        more = {"Документ": [{"Идентификатор": "one"}], "Навигация": {"ЕстьЕще": "Да"}}
        last = {"Документ": [{"Идентификатор": "two"}], "Навигация": {"ЕстьЕще": "Нет"}}
        c = client(more, last)
        result = c.list_documents({"Тип": "x", "Навигация": {"Страница": "10000"}}, True, max_pages=2)
        self.assertEqual(len(result["Документ"]), 2)
        self.assertEqual([x[1]["params"]["Фильтр"]["Навигация"]["Страница"] for x in c.transport.calls],
                         ["10000", "10001"])
        c = client(more, more, last)
        with self.assertRaises(common.ClientError) as ctx:
            c.list_documents({"Тип": "x"}, True, max_pages=2)
        self.assertEqual(ctx.exception.code, "pagination_limit")
        self.assertEqual(len(c.transport.calls), 2)

    def test_invalid_pagination_budget_prevents_requests(self):
        for limit in (0, -1, 1.5, True):
            c = client()
            with self.subTest(limit=limit), self.assertRaises(common.ClientError):
                c.list_documents({"Тип": "x"}, True, max_pages=limit)
            self.assertFalse(c.transport.calls)

    def test_cli_passes_pagination_budget(self):
        last = {"Документ": [], "Навигация": {"ЕстьЕще": "Нет"}}
        c = client(last)
        args = cli.parser().parse_args(["list", "--filter", "-", "--all", "--max-pages", "3"])
        with patch("sys.stdin", io.StringIO('{"Тип":"x"}')), patch.object(
                c, "list_documents", wraps=c.list_documents) as listing:
            self.assertEqual(cli.run(args, c)["Документ"], [])
        listing.assert_called_once_with({"Тип": "x"}, True, 3)

    def test_empty_single_and_all_pages(self):
        empty = {"Документ": [], "Навигация": {"ЕстьЕще": "Нет"}}
        c = client(empty)
        self.assertEqual(c.list_documents({"Тип": "ДокОтгрИсх"}), empty)
        self.assertEqual(len(c.transport.calls), 1)
        c = client({"Документ": {"Идентификатор": "one"}, "Навигация": {"ЕстьЕще": "Да"}}, empty)
        filters = {"Тип": "ДокОтгрИсх", "Навигация": {"РазмерСтраницы": "50"}}
        result = c.list_documents(filters, all_pages=True)
        self.assertEqual(result["Документ"], [{"Идентификатор": "one"}])
        self.assertEqual([x[1]["params"]["Фильтр"]["Навигация"]["Страница"] for x in c.transport.calls], ["0", "1"])
        self.assertNotIn("Страница", filters["Навигация"])

    def test_inconsistent_pagination_stops(self):
        c = client({"Документ": [], "Навигация": {"ЕстьЕще": "Да"}})
        with self.assertRaises(common.ClientError):
            c.list_documents({"Тип": "x"}, True)
        self.assertEqual(len(c.transport.calls), 1)

    def test_update_preserves_files_and_signatures(self):
        c = client(draft())
        result = drafts.prepare_draft(c, {"Примечание": "Обновлено"}, "doc")
        self.assertEqual(result["Номер"], "42")
        self.assertEqual(result["Идентификатор"], "doc")
        self.assertEqual(result["Примечание"], "Обновлено")
        self.assertNotIn("Состояние", result)
        attachment = result["Вложение"][0]
        self.assertEqual(base64.b64decode(attachment["Файл"]["ДвоичныеДанные"]), b"\x00\xffbinary")
        self.assertIn("Подпись", attachment)
        self.assertEqual(len(c.transport.downloads), 2)
        self.assertEqual(len(c.transport.calls), 1)

    def test_update_preserves_signature_attorneys_and_filters_read_only_fields(self):
        attorney = {"ИдентификаторМЧД": "mchd-id", "СистемаХраненияМЧД": "storage",
                    "ДействительнаС": "01.10.2026 00.00.00", "ДействительнаПо": "31.10.2026 23.59.59",
                    "ВнутрНомер": "42", "ИННДоверителя": "1234567890", "ИННПредставителя": "123456789012"}
        for singleton in (True, False):
            with self.subTest(singleton=singleton):
                current = draft()
                source = dict(attorney, ТолькоДляЧтения="discard")
                current["Вложение"][0]["Подпись"]["Сертификат"] = {
                    "ФИО": "Не передавать", "Доверенность": source if singleton else [source]}
                original = copy.deepcopy(current)
                result = drafts.prepare_draft(client(current), {"Примечание": "changed"}, "doc")
                signature = result["Вложение"][0]["Подпись"][0]
                self.assertEqual(signature["Сертификат"], {"Доверенность": [attorney]})
                self.assertEqual(base64.b64decode(signature["Файл"]["ДвоичныеДанные"]), b"\x00\xffbinary")
                self.assertEqual(current, original)

    def test_update_requires_explicit_unlocked_extension(self):
        for extension in (None, {}, {"ЗакрытОтИзменений": "Да"},
                          {"ЗакрытОтИзменений": "unknown"}, "invalid"):
            with self.subTest(extension=extension):
                current = draft()
                if extension is None:
                    current.pop("Расширение")
                else:
                    current["Расширение"] = extension
                c = client(current)
                with self.assertRaises(common.ClientError) as ctx:
                    drafts.prepare_draft(c, {}, "doc")
                self.assertEqual(ctx.exception.code, "not_draft")
                self.assertEqual(c.transport.calls[0][1]["params"]["Документ"],
                                 {"Идентификатор": "doc", "ДопПоля": "Расширение"})
                self.assertFalse(c.transport.downloads)

    def test_refuse_non_draft_missing_state_incoming_deleted_partial_or_locked(self):
        changes = [{"Состояние": {"Код": "3"}}, {"Состояние": {}}, {"Направление": "Входящий"},
                   {"Удален": "Да"}, {"ЧастичныеДанные": "Да"},
                   {"Расширение": {"ЗакрытОтИзменений": "Да"}}]
        for change in changes:
            with self.subTest(change=change):
                current = draft()
                current.update(change)
                c = client(current)
                with self.assertRaises(common.ClientError):
                    drafts.prepare_draft(c, {"Номер": "43"}, "doc")
                self.assertEqual(len(c.transport.calls), 1)
                self.assertFalse(c.transport.downloads)

    def test_replace_removes_invalid_signatures_and_old_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.pdf"
            path.write_bytes(b"new")
            current = draft()
            current["Вложение"][0]["Подпись"]["Сертификат"] = {
                "Доверенность": [{"ИдентификаторМЧД": "old-mchd"}]}
            c = client(current)
            result = drafts.prepare_draft(c, {}, "doc", replacements=[f"a1={path}"])
            a = result["Вложение"][0]
            self.assertNotIn("Подпись", a)
            self.assertEqual(base64.b64decode(a["Файл"]["ДвоичныеДанные"]), b"new")
            self.assertFalse(c.transport.downloads)

    def test_explicit_remove_and_unknown_identifier(self):
        c = client(draft())
        result = drafts.prepare_draft(c, {}, "doc", removals=["a1"])
        self.assertEqual(result["Вложение"], [])
        with self.assertRaises(common.ClientError):
            drafts.prepare_draft(client(draft()), {}, "doc", removals=["missing"])

    def test_preview_create_is_offline_and_update_only_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "draft.json"
            src.write_text('{"Тип":"ДокОтгрИсх","Номер":"Тест-1"}', encoding="utf-8")
            args = cli.parser().parse_args(["draft-create", "--input", str(src), "--dry-run"])
            c = client()
            self.assertTrue(cli.run(args, c)["dry_run"])
            self.assertFalse(c.transport.calls)
            args = cli.parser().parse_args(["draft-update", "doc", "--input", str(src), "--dry-run"])
            c = client(draft())
            result = cli.run(args, c)
            self.assertEqual(len(c.transport.calls), 1)
            self.assertIn("base64_characters", result["params"]["Документ"]["Вложение"][0]["Файл"]["ДвоичныеДанные"])

    def test_create_write_and_stdin(self):
        args = cli.parser().parse_args(["draft-create", "--input", "-"])
        c = client({"Идентификатор": "created"})
        with patch("sys.stdin", io.StringIO('{"Тип":"ДокОтгрИсх"}')):
            self.assertEqual(cli.run(args, c)["Идентификатор"], "created")
        self.assertEqual(c.transport.calls[0][1]["method"], "СБИС.ЗаписатьДокумент")

    def test_prevent_addressing_create_and_workflow_fields(self):
        for data in [{"Идентификатор": "x"}, {"Состояние": {"Код": "0"}}, {"Этап": {}}]:
            with self.assertRaises(common.ClientError):
                drafts.prepare_draft(client(), data)

    def test_download_binary_signatures_and_no_overwrite_or_traversal(self):
        doc = draft()
        doc["Вложение"][0]["Файл"]["Имя"] = "../../secret.bin"
        doc["Вложение"][0]["Подпись"]["Файл"]["Имя"] = "C:\\temp\\secret.bin"
        c = client(doc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "output"
            root.mkdir()
            (root / "secret.bin").symlink_to(Path(directory) / "outside")
            result = c.download("doc", root)
            self.assertEqual(len(result["files"]), 2)
            paths = [Path(f["path"]) for f in result["files"]]
            self.assertEqual(len(set(paths)), 2)
            for p in paths:
                self.assertEqual(p.parent, root)
                self.assertEqual(p.read_bytes(), b"\x00\xffbinary")
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertFalse((Path(directory) / "outside").exists())

    def test_unicode_filename_fits_filesystem_byte_limit(self):
        name = api.safe_name("Документ" * 40 + ".pdf")
        self.assertLessEqual(len(name.encode("utf-8")), 180)
        self.assertTrue(name)

    def test_download_url_validation(self):
        for url in ["http://disk.sbis.ru/a", "https://sbis.ru.evil.test/a", "file:///etc/passwd",
                    "https://evil.test/a", "https://u:p@disk.sbis.ru/a"]:
            with self.subTest(url=url), self.assertRaises(common.ClientError):
                http_transport.file_url(url)
        self.assertEqual(http_transport.file_url("https://cdn-disk.saby.ru/a"), "https://cdn-disk.saby.ru/a")

    def test_update_rechecks_before_write_and_rejects_intervening_change(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "patch.json"
            src.write_text('{"Примечание":"updated"}', encoding="utf-8")
            args = cli.parser().parse_args(["draft-update", "doc", "--input", str(src)])
            c = client(draft(), draft(), {"Идентификатор": "doc"})
            self.assertEqual(cli.run(args, c), {"Идентификатор": "doc"})
            self.assertEqual([x[1]["method"] for x in c.transport.calls],
                             ["СБИС.ПрочитатьДокумент", "СБИС.ПрочитатьДокумент", "СБИС.ЗаписатьДокумент"])
            for call in c.transport.calls[:2]:
                self.assertEqual(call[1]["params"]["Документ"],
                                 {"Идентификатор": "doc", "ДопПоля": "Расширение"})
            changed = draft()
            changed["Состояние"] = {"Код": "3"}
            c = client(draft(), changed)
            with self.assertRaises(common.ClientError) as ctx:
                cli.run(args, c)
            self.assertEqual(ctx.exception.code, "document_changed")
            self.assertEqual(len(c.transport.calls), 2)

    def test_update_recheck_refuses_locked_or_missing_extension(self):
        args = cli.parser().parse_args(["draft-update", "doc", "--input", "-"])
        for extension in ({"ЗакрытОтИзменений": "Да"}, {}, None):
            with self.subTest(extension=extension):
                changed = draft()
                if extension is None:
                    changed.pop("Расширение")
                else:
                    changed["Расширение"] = extension
                c = client(draft(), changed)
                with patch("sys.stdin", io.StringIO('{"Примечание":"changed"}')):
                    with self.assertRaises(common.ClientError) as ctx:
                        cli.run(args, c)
                self.assertEqual(ctx.exception.code, "not_draft")
                self.assertEqual(len(c.transport.calls), 2)

    def test_regular_read_does_not_request_extension(self):
        current = draft()
        current.pop("Расширение")
        c = client(current)
        self.assertEqual(c.read("doc"), current)
        self.assertEqual(c.transport.calls[0][1]["params"], {"Документ": {"Идентификатор": "doc"}})

    def test_preview_rejects_oversized_request(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "draft.json"
            src.write_text('{"Тип":"ДокОтгрИсх","Примечание":"text"}', encoding="utf-8")
            args = cli.parser().parse_args(["draft-create", "--input", str(src), "--dry-run"])
            with patch.object(cli, "MAX_REQUEST", 10), self.assertRaises(common.ClientError):
                cli.run(args, client())

    def test_check_uses_edo_organizations_without_exposing_details(self):
        for rows in ([], [{"СвЮЛ": {"Название": "private company", "ИНН": "1234567890"}}]):
            with self.subTest(rows=rows):
                c = client({"НашаОрганизация": rows})
                out = io.StringIO()
                with patch.object(cli, "Client", return_value=c), redirect_stdout(out):
                    self.assertEqual(cli.main(["check"]), 0)
                self.assertEqual(json.loads(out.getvalue()), {"ok": True, "profile": "production"})
                self.assertEqual(len(c.transport.calls), 1)
                self.assertEqual(c.transport.calls[0][1]["method"], "СБИС.СписокНашихОрганизаций")
                self.assertEqual(c.transport.calls[0][1]["params"], {"Фильтр": {}})

    def test_check_reports_access_failure_without_success_output(self):
        c = client((403, b"private server response"))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "Client", return_value=c), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["check"]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "access_denied")
        self.assertEqual(len(c.transport.calls), 1)

    def test_check_organizations_read_and_main_json(self):
        for argv, results, expected in [
                (["check"], [{"sid": "must-not-print"}], {"ok": True, "profile": "production"}),
                (["organizations"], [{"НашаОрганизация": []}], {"НашаОрганизация": []}),
                (["read", "doc"], [{"Документ": draft()}], draft())]:
            with self.subTest(argv=argv):
                c = client(*results)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(cli, "Client", return_value=c), redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(cli.main(argv), 0)
                self.assertEqual(json.loads(stdout.getvalue()), expected)
                self.assertEqual(stderr.getvalue(), "")


class HttpTransportTests(unittest.TestCase):
    def test_size_diagnostics_follow_configured_limits(self):
        transport = http_transport.HttpTransport()
        with patch.object(http_transport, "MAX_REQUEST", 1024 * 1024), self.assertRaises(common.ClientError) as ctx:
            transport.post("https://online.sbis.ru", {"data": "x" * (1024 * 1024)}, {})
        self.assertIn("1 МиБ", str(ctx.exception))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.bin"
            path.write_bytes(b"x" * (1024 * 1024 + 1))
            with patch.object(drafts, "MAX_FILE", 1024 * 1024), self.assertRaises(common.ClientError) as ctx:
                drafts.local_attachment(path)
            self.assertIn("1 МиБ", str(ctx.exception))

    def test_entry_point_from_another_directory(self):
        script = Path(__file__).parents[1] / "scripts" / "sbis.py"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(script.resolve()), "draft-create", "--input", "-", "--dry-run"],
                cwd=directory, input='{"Тип":"ДокОтгрИсх"}', capture_output=True, text=True,
                env={"PATH": os.defpath, "HOME": directory}, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["dry_run"])
        self.assertEqual(result.stderr, "")

    class Response:
        status = 200

        def __init__(self, content=b"binary"):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return self.content[:limit]

    def test_real_transport_encodes_utf8_and_sets_timeout(self):
        transport = http_transport.HttpTransport(12)
        with patch.object(transport.opener, "open", return_value=self.Response(b'{}')) as open_mock:
            transport.post("https://online.sbis.ru/service/?srv=1", {"Номер": "Счёт"},
                           {"X-SBISAccessToken": "secret"})
            req = open_mock.call_args.args[0]
            self.assertIn("Счёт", req.data.decode("utf-8"))
            self.assertEqual(open_mock.call_args.kwargs["timeout"], 12)

    def test_download_redirect_revalidation(self):
        transport = http_transport.HttpTransport()
        redirected = urllib.error.HTTPError("https://disk.sbis.ru/a", 302, "redirect",
                                           {"Location": "https://evil.test/leak"}, None)
        with patch.object(transport.opener, "open", side_effect=redirected) as open_mock:
            with self.assertRaises(common.ClientError) as ctx:
                transport.get("https://disk.sbis.ru/a")
            self.assertEqual(ctx.exception.code, "unsafe_download")
            self.assertEqual(open_mock.call_count, 1)
            self.assertEqual(open_mock.call_args.args, ("https://disk.sbis.ru/a",))
        self.assertTrue(redirected.closed)

    def test_transport_closes_http_errors_on_post_and_download(self):
        for operation in ("post", "get"):
            with self.subTest(operation=operation):
                body = io.BytesIO(b"private")
                error = urllib.error.HTTPError("https://disk.sbis.ru/a", 403, "private", {}, body)
                self.addCleanup(error.close)
                transport = http_transport.HttpTransport()
                with patch.object(transport.opener, "open", side_effect=error) as opened:
                    with self.assertRaises(urllib.error.HTTPError):
                        if operation == "post":
                            transport.post("https://online.sbis.ru/service/?srv=1", {}, {})
                        else:
                            transport.get("https://disk.sbis.ru/a")
                self.assertTrue(body.closed)
                self.assertEqual(opened.call_count, 1)

    def test_download_closes_redirect_before_following(self):
        body = io.BytesIO(b"redirect")
        error = urllib.error.HTTPError("https://disk.sbis.ru/a", 302, "redirect",
                                       {"Location": "/b"}, body)
        self.addCleanup(error.close)
        transport = http_transport.HttpTransport()
        def open_response(url, **kwargs):
            if url == "https://disk.sbis.ru/a":
                raise error
            self.assertEqual(url, "https://disk.sbis.ru/b")
            self.assertTrue(body.closed)
            return self.Response()
        with patch.object(transport.opener, "open", side_effect=open_response) as opened:
            self.assertEqual(transport.get("https://disk.sbis.ru/a"), b"binary")
        self.assertEqual(opened.call_count, 2)

    def test_rpc_redirects_are_not_followed(self):
        self.assertIsNone(http_transport.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test"))

    def test_malformed_input_and_cli_diagnostics_are_private(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(["--unknown-secret-arg", "password-value"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("password-value", stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
