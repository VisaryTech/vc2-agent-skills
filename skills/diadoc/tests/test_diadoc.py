"""Offline behavioral tests. No live credentials or production requests."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
from http.client import IncompleteRead
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
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from diadoc_lib.auth import ENV_KEYS, TOKEN_URL, TokenProvider, load_credentials
from diadoc_lib.client import Client
from diadoc_lib.cli import main, parser, run
from diadoc_lib.common import ClientError, redact
from diadoc_lib.drafts import copy_draft, prepare
from diadoc_lib.transport import HttpTransport, NoRedirect

BOX = "11111111-1111-1111-1111-111111111111"
TO = "22222222-2222-2222-2222-222222222222"
MSG = "33333333-3333-3333-3333-333333333333"
EID = "44444444-4444-4444-4444-444444444444"
SIG = "55555555-5555-5555-5555-555555555555"
OP = "66666666-6666-6666-6666-666666666666"
NEW = "77777777-7777-7777-7777-777777777777"
TOKEN = "private-token-123"


def attachment():
    return {"TypeNamedId": "Nonformalized", "SignedContent": {"Content": "YWJj"},
            "Metadata": [{"Key": "FileName", "Value": "document.pdf"}]}


def payload():
    return {"FromBoxId": BOX, "ToBoxId": TO, "DocumentAttachments": [attachment()]}


def source():
    return {"MessageId": MSG, "FromBoxId": BOX.replace("-", "") + "@diadoc.ru", "ToBoxId": TO,
            "IsDraft": True, "DraftIsRecycled": False, "Entities": [
                {"EntityId": EID, "EntityType": "Attachment", "FileName": "document.pdf",
                 "NeedRecipientSignature": True, "DocumentInfo": {
                     "TypeNamedId": "Nonformalized", "Function": "default", "Version": "v1",
                     "Metadata": [{"Key": "FileName", "Value": "document.pdf"}],
                     "CustomData": [{"Key": "purpose", "Value": "sample"}], "WorkflowId": 2,
                     "FromDepartmentId": BOX, "ToDepartmentId": TO,
                     "Participants": [{"Participant": {"Role": "default:sender"}},
                                      {"Participant": {"Role": "default:recipient"}}]}}]}


class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, payload, headers):
        self.calls.append((method, url, copy.deepcopy(payload), headers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return response
        return 200, {}, response if isinstance(response, bytes) else json.dumps(response).encode()


class Tests(unittest.TestCase):
    def credentials(self):
        return dict(zip(ENV_KEYS, ("application", "secret+&= ключ", "refresh-original")))

    def token_response(self, **extra):
        return {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 120, **extra}

    def test_refresh_flow_reuses_and_renews_token(self):
        transport = Fake(self.token_response(refresh_token="rotated"), {}, {},
                         self.token_response(access_token="new-token"), {})
        provider = TokenProvider(self.credentials(), transport)
        client = Client(transport=transport, token_provider=provider)
        with patch("diadoc_lib.auth.time.monotonic", return_value=100):
            client.organizations()
        with patch("diadoc_lib.auth.time.monotonic", return_value=159):
            client.organizations()
        with patch("diadoc_lib.auth.time.monotonic", return_value=160):
            client.organizations()
        self.assertEqual(len(transport.calls), 5)
        first = transport.calls[0]
        self.assertEqual(first[:2], ("POST", TOKEN_URL))
        self.assertEqual(first[2], {"grant_type": "refresh_token", "client_id": "application",
                                   "client_secret": "secret+&= ключ", "refresh_token": "refresh-original"})
        self.assertNotIn("Authorization", first[3])
        self.assertEqual(transport.calls[3][2]["refresh_token"], "rotated")
        self.assertEqual(transport.calls[4][3]["Authorization"], "Bearer new-token")
        self.assertIn(TOKEN, provider.secrets)

    def test_refresh_missing_credentials_and_legacy_token(self):
        transport = Fake()
        for key in ENV_KEYS:
            for value in (None, "", "  "):
                credentials = self.credentials()
                credentials[key] = value
                self.error("credentials_required", TokenProvider(credentials, transport).get_token)
        self.error("credentials_required", TokenProvider({"DIADOC_ACCESS_TOKEN": TOKEN}, transport).get_token)
        self.assertFalse(transport.calls)

    def test_refresh_errors_do_not_call_diadoc_or_retry(self):
        cases = [(response, "authentication_failed") for response in ((400, {}, b"secret"),
                 (401, {}, b""), (403, {}, b""), (302, {}, b""), (500, {}, b""))]
        cases += [(TimeoutError(), "network_error"), (IncompleteRead(b"private", 10), "network_error")]
        cases += [(response, "invalid_response") for response in (b"bad", b"\xff", [], {},
                  self.token_response(access_token=""), self.token_response(token_type="other"),
                  self.token_response(expires_in=True), self.token_response(expires_in=0),
                  self.token_response(expires_in=float("inf")), self.token_response(refresh_token=""))]
        for key in ("access_token", "token_type", "expires_in"):
            response = self.token_response()
            del response[key]
            cases.append((response, "invalid_response"))
        for response, code in cases:
            with self.subTest(response=response):
                transport = Fake(response)
                provider = TokenProvider(self.credentials(), transport)
                self.error(code, Client(transport=transport, token_provider=provider).organizations)
                self.assertEqual(len(transport.calls), 1)

    def test_form_transport_preserves_special_characters(self):
        transport = HttpTransport(12)
        response = io.BytesIO(b"{}")
        response.status, response.headers = 200, {}
        values = {"client_secret": "a+&= ключ", "refresh_token": "x/y#z"}
        with patch.object(transport.opener, "open", return_value=response) as opened:
            transport.request("POST", TOKEN_URL, values, {"Content-Type": "application/x-www-form-urlencoded"})
        request = opened.call_args.args[0]
        self.assertEqual(urllib.parse.parse_qs(request.data.decode()), {k: [v] for k, v in values.items()})
        self.assertEqual(opened.call_args.kwargs["timeout"], 12)

    def test_cli_refresh_and_all_secrets_redacted(self):
        credentials = self.credentials()
        transport = Fake(self.token_response(refresh_token="rotated", id_token="identity-token"),
                         {"nested": ["prefix " + " ".join(credentials.values()) + " " + TOKEN +
                                     " rotated identity-token suffix"], "client_secret": "unrecognized"})
        out, err = io.StringIO(), io.StringIO()
        with patch("diadoc_lib.cli.load_credentials", return_value=credentials), \
                patch("diadoc_lib.cli.HttpTransport", return_value=transport), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["organizations"]), 0)
        for secret in (*credentials.values(), TOKEN, "rotated", "identity-token", "unrecognized"):
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
        self.assertEqual(len(transport.calls), 2)

    def test_cli_error_redacts_nested_secrets_and_tokens(self):
        credentials = self.credentials()
        transport = Fake(self.token_response(refresh_token="rotated"))
        def fail(client):
            client.token_provider.get_token()
            raise ClientError("test_error", "prefix " + TOKEN,
                              nested=["rotated " + credentials["DIADOC_CLIENT_SECRET"]])
        out, err = io.StringIO(), io.StringIO()
        with patch("diadoc_lib.cli.load_credentials", return_value=credentials), \
                patch("diadoc_lib.cli.HttpTransport", return_value=transport), \
                patch.object(Client, "organizations", fail), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["check"]), 1)
        self.assertFalse(out.getvalue())
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "test_error")
        for secret in (TOKEN, "rotated", credentials["DIADOC_CLIENT_SECRET"]):
            self.assertNotIn(secret, err.getvalue())

    def test_all_env_credentials_priority_and_legacy_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.env"
            credentials = self.credentials()
            path.write_text("\n".join(f"{key}='{value}'" for key, value in credentials.items()) +
                            "\nDIADOC_ACCESS_TOKEN=ignored\n")
            self.assertEqual(load_credentials({}, path), credentials)
            for key in ENV_KEYS:
                self.assertEqual(load_credentials({key: ""}, path), {**credentials, key: ""})
                self.assertEqual(load_credentials({key: "process"}, path), {**credentials, key: "process"})

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(ClientError) as raised:
            fn(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_auth_header_and_organizations(self):
        transport = Fake({"Organizations": [{"Boxes": [{"BoxId": BOX}]}]})
        result = Client(TOKEN, transport=transport).organizations()
        self.assertEqual(result["Organizations"][0]["Boxes"][0]["BoxId"], BOX)
        self.assertEqual(transport.calls[0][3]["Authorization"], "Bearer " + TOKEN)
        self.assertTrue(transport.calls[0][1].endswith("/GetMyOrganizations"))

    def test_missing_token_and_box_prevent_network(self):
        transport = Fake()
        self.error("credentials_required", Client("", transport=transport).organizations)
        self.error("invalid_input", Client(TOKEN, transport=transport).message, MSG)
        self.assertFalse(transport.calls)

    def test_box_id_normalization(self):
        self.assertEqual(Client(TOKEN, BOX.replace("-", "") + "@diadoc.ru").box(), BOX)
        self.error("invalid_input", Client, TOKEN, "garbage")

    def test_env_priority_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.env"
            path.write_text("DIADOC_REFRESH_TOKEN='file#token' # ok\n")
            self.assertEqual(load_credentials({}, path)["DIADOC_REFRESH_TOKEN"], "file#token")
            self.assertEqual(load_credentials({"DIADOC_REFRESH_TOKEN": ""}, path)["DIADOC_REFRESH_TOKEN"], "")
            self.assertEqual(load_credentials({"DIADOC_REFRESH_TOKEN": TOKEN}, path)["DIADOC_REFRESH_TOKEN"], TOKEN)

    def test_env_not_executed_and_invalid_file_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.env"
            path.write_text('DIADOC_REFRESH_TOKEN=$(echo danger)\n')
            self.assertEqual(load_credentials({}, path)["DIADOC_REFRESH_TOKEN"], "$(echo danger)")
            path.write_text(TOKEN)
            exc = self.error("config_error", load_credentials, {}, path)
            self.assertNotIn(TOKEN, str(exc))
            self.error("config_error", load_credentials, {}, Path(tmp) / "absent")

    def test_http_errors_no_retries(self):
        for status, code in ((401, "token_expired"), (402, "subscription_required"),
                             (403, "access_denied"), (404, "not_found"), (302, "http_error")):
            with self.subTest(status=status):
                transport = Fake((status, {}, TOKEN.encode()))
                exc = self.error(code, Client(TOKEN, transport=transport).organizations)
                self.assertNotIn(TOKEN, str(exc))
                self.assertEqual(len(transport.calls), 1)

    def test_network_and_invalid_json(self):
        self.error("network_error", Client(TOKEN, transport=Fake(TimeoutError())).organizations)
        for raw in (b"no-json", b"[]", b"\xff"):
            self.error("invalid_response", Client(TOKEN, transport=Fake(raw)).organizations)

    def test_list_single_page_and_post_contract(self):
        transport = Fake({"Documents": [{"IndexKey": "a"}]})
        Client(TOKEN, BOX, transport).documents({"DocumentCategory": "Incoming"})
        method, url, body, _ = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/V4/GetDocuments?boxId=" + BOX, url)
        self.assertEqual(body, {"DocumentCategory": "Incoming"})
        self.assertEqual(len(transport.calls), 1)

    def test_pagination_cursor_and_immutable_filter(self):
        transport = Fake({"Documents": [{"IndexKey": "a"}]}, {"Documents": [{"IndexKey": "b"}]}, {"Documents": []})
        filters = {"DocumentCategory": "Incoming", "Count": 1}
        result = Client(TOKEN, BOX, transport).documents(filters, True)
        self.assertEqual(len(result["Documents"]), 2)
        self.assertEqual(transport.calls[1][2]["AfterIndexKey"], "a")
        self.assertNotIn("AfterIndexKey", filters)

    def test_pagination_stalled(self):
        transport = Fake({"Documents": [{"IndexKey": "a"}]}, {"Documents": [{"IndexKey": "a"}]})
        self.error("pagination_stalled", Client(TOKEN, BOX, transport).documents,
                   {"DocumentCategory": "Incoming", "Count": 1}, True)

    def test_pagination_limit_and_boundary(self):
        client = Client(TOKEN, BOX, Fake({"Documents": [{"IndexKey": "a"}]}))
        self.error("pagination_limit", client.documents, {"DocumentCategory": "Incoming", "Count": 1}, True, 1)
        result = Client(TOKEN, BOX, Fake({"Documents": []})).documents({"DocumentCategory": "Incoming"}, True, 1)
        self.assertTrue(result["all_pages"])

    def test_list_validation_and_bad_documents(self):
        client = Client(TOKEN, BOX, Fake())
        for value in ({}, {"DocumentCategory": "Incoming", "Count": 0}, {"DocumentCategory": "Incoming", "Count": True}):
            self.error("invalid_input", client.documents, value)
        self.error("invalid_response", Client(TOKEN, BOX, Fake({"Documents": [1]})).documents, {"DocumentCategory": "Incoming"})

    def test_read_message_and_document_keep_statuses(self):
        message = source()
        document = {"DocflowStatus": {"PrimaryStatus": {"StatusText": "Ожидается подпись"}}}
        transport = Fake(message, document)
        result = Client(TOKEN, BOX, transport).read(MSG, EID)
        self.assertEqual(result["Document"], document)
        self.assertIn("/V6/GetMessage?", transport.calls[0][1])
        self.assertIn("/V3/GetDocument?", transport.calls[1][1])
        self.assertIn("injectEntityContent=false", transport.calls[0][1])

    def test_download_files_signatures_sanitization_and_collision(self):
        message = source()
        message["Entities"][0]["FileName"] = "../../evil.pdf"
        message["Entities"].append({"EntityId": SIG, "EntityType": "Signature", "ParentEntityId": EID})
        transport = Fake(message, b"document", b"signature", message, b"document", b"signature")
        client = Client(TOKEN, BOX, transport)
        with tempfile.TemporaryDirectory() as tmp:
            first = client.download(MSG, tmp)
            second = client.download(MSG, tmp)
            self.assertEqual(len(first["files"]), 2)
            for file in first["files"] + second["files"]:
                path = Path(file["path"])
                self.assertEqual(path.parent, Path(tmp))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotEqual(first["files"][0]["path"], second["files"][0]["path"])
            self.assertTrue(Path(first["files"][1]["path"]).name.endswith(".sig"))
            self.assertIn("/V4/GetEntityContent?", transport.calls[1][1])

    def test_download_existing_symlink_not_followed(self):
        message = source()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "export"
            root.mkdir()
            target = Path(tmp) / "target"
            target.write_bytes(b"keep")
            (root / "document.pdf").symlink_to(target)
            Client(TOKEN, BOX, Fake(message, b"new")).download(MSG, root)
            self.assertEqual(target.read_bytes(), b"keep")
            self.assertEqual((root / "1_document.pdf").read_bytes(), b"new")

    def test_download_filtered_request(self):
        transport = Fake(source(), b"abc")
        with tempfile.TemporaryDirectory() as tmp:
            Client(TOKEN, BOX, transport).download(MSG, tmp, EID)
        self.assertIn("entityId=" + EID, transport.calls[0][1])

    def test_download_long_unicode_names_and_collisions(self):
        for name in ("я" * 130 + ".pdf", "文" * 100 + ".pdf", "📄" * 100 + ".pdf"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                message = source()
                message["Entities"][0]["FileName"] = name
                client = Client(TOKEN, BOX, Fake(message, b"first", message, b"second"))
                first = Path(client.download(MSG, tmp)["files"][0]["path"])
                second = Path(client.download(MSG, tmp)["files"][0]["path"])
                self.assertEqual(first.read_bytes(), b"first")
                self.assertEqual(second.read_bytes(), b"second")
                self.assertNotEqual(first, second)
                self.assertLessEqual(len(first.name.encode("utf-8")), 180)
                self.assertLessEqual(len(second.name.encode("utf-8")), 185)
                self.assertNotIn("\ufffd", first.name)

    def test_copy_rejects_mixed_head_and_branch_departments(self):
        for key in ("FromDepartmentId", "ToDepartmentId"):
            for head in (None, "", "00000000-0000-0000-0000-000000000000"):
                with self.subTest(key=key, head=head):
                    original = source()
                    other = copy.deepcopy(original["Entities"][0])
                    other["EntityId"] = SIG
                    if head is None:
                        other["DocumentInfo"].pop(key)
                    else:
                        other["DocumentInfo"][key] = head
                    original["Entities"].append(other)
                    transport = Fake(original, b"first", b"second")
                    self.error("unsupported_copy", copy_draft,
                               Client(TOKEN, BOX, transport), MSG, {})
                    self.assertTrue(all(call[0] == "GET" for call in transport.calls))

    def test_copy_equivalent_head_department_values(self):
        original = source()
        for index, head in enumerate((None, "", "00000000-0000-0000-0000-000000000000")):
            entity = copy.deepcopy(original["Entities"][0])
            entity["EntityId"] = (EID, SIG, NEW)[index]
            for key in ("FromDepartmentId", "ToDepartmentId"):
                if head is None:
                    entity["DocumentInfo"].pop(key)
                else:
                    entity["DocumentInfo"][key] = head
            if index == 0:
                original["Entities"] = []
            original["Entities"].append(entity)
        result = copy_draft(Client(TOKEN, BOX, Fake(original, b"a", b"b", b"c")), MSG, {})
        self.assertEqual(len(result["DocumentAttachments"]), 3)
        for key in ("FromDepartmentId", "ToDepartmentId"):
            self.assertEqual(result[key], "00000000-0000-0000-0000-000000000000")

    def test_prepare_file_content_and_no_input_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.pdf"
            path.write_bytes(b"real-file")
            value = payload()
            value["DocumentAttachments"][0].pop("SignedContent")
            value["DocumentAttachments"][0]["FilePath"] = str(path)
            result = prepare(value, BOX)
            self.assertTrue(result["IsDraft"])
            self.assertEqual(base64.b64decode(result["DocumentAttachments"][0]["SignedContent"]["Content"]), b"real-file")
            self.assertNotIn("FilePath", result["DocumentAttachments"][0])
            self.assertIn("FilePath", value["DocumentAttachments"][0])

    def test_prepare_rejects_send_signatures_unknown_fields(self):
        for field, value in (("IsDraft", False), ("DelaySend", True), ("FromBoxId", TO), ("Invoices", [])):
            p = payload()
            p[field] = value
            self.error("invalid_input", prepare, p, BOX)
        for bad in ({"Signature": "c2ln"}, {"PowerOfAttorney": {}}, {"Content": "!"}):
            p = payload()
            p["DocumentAttachments"][0]["SignedContent"] = bad
            self.error("invalid_input", prepare, p, BOX)

    def test_prepare_content_conflict_and_missing_file(self):
        p = payload()
        p["DocumentAttachments"][0]["FilePath"] = "/absent"
        self.error("invalid_input", prepare, p, BOX)
        p["DocumentAttachments"][0].pop("SignedContent")
        self.error("file_error", prepare, p, BOX)

    def test_copy_preserves_content_metadata_and_source(self):
        original = source()
        transport = Fake(original, b"original")
        result = copy_draft(Client(TOKEN, BOX, transport), MSG, {"Message": {"ToBoxId": NEW}})
        self.assertEqual(result["ToBoxId"], NEW)
        self.assertEqual(result["FromDepartmentId"], BOX)
        copied = result["DocumentAttachments"][0]
        self.assertEqual(copied["WorkflowId"], 2)
        self.assertEqual(copied["CustomData"], original["Entities"][0]["DocumentInfo"]["CustomData"])
        self.assertTrue(copied["NeedRecipientSignature"])
        self.assertEqual(base64.b64decode(copied["SignedContent"]["Content"]), b"original")
        self.assertEqual(original, source())
        self.assertTrue(all(c[0] == "GET" for c in transport.calls))

    def test_copy_current_message_type_and_no_recipient(self):
        original = source()
        original.pop("IsDraft")
        original["MessageType"] = "Draft"
        original.pop("ToBoxId")
        result = copy_draft(Client(TOKEN, BOX, Fake(original, b"file")), MSG, {})
        self.assertTrue(result["IsDraft"])
        self.assertNotIn("ToBoxId", result)
        original["IsDraft"] = True
        original["MessageType"] = "Letter"
        self.error("not_draft", copy_draft, Client(TOKEN, BOX, Fake(original)), MSG, {})

    def test_copy_write_returns_both_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "patch.json"
            path.write_text("{}")
            args = parser().parse_args(["--box-id", BOX, "draft-copy", MSG, "--input", str(path), "--operation-id", OP])
            transport = Fake(source(), b"file", {"MessageId": NEW, "IsDraft": True})
            result = run(args, Client(TOKEN, BOX, transport))
            self.assertEqual(result["source_message_id"], MSG)
            self.assertEqual(result["new_message_id"], NEW)
            self.assertEqual(len([c for c in transport.calls if c[0] == "POST"]), 1)
            self.assertIn("/V3/PostMessage?", transport.calls[-1][1])

    def test_copy_add_replace_remove(self):
        original = source()
        other = copy.deepcopy(original["Entities"][0])
        other["EntityId"] = SIG
        original["Entities"].append(other)
        transport = Fake(original)
        result = copy_draft(Client(TOKEN, BOX, transport), MSG,
                            {"Replace": {EID: {"SignedContent": {"Content": "bmV3"}}},
                             "Remove": [SIG], "Add": [attachment()]})
        self.assertEqual(len(result["DocumentAttachments"]), 2)
        self.assertEqual(base64.b64decode(result["DocumentAttachments"][0]["SignedContent"]["Content"]), b"new")
        self.assertEqual(len(transport.calls), 1)

    def test_copy_comment_preserved(self):
        original = source()
        original["Entities"].append({"EntityType": "Attachment", "EntityId": SIG,
                                     "ParentEntityId": EID, "AttachmentType": "AttachmentComment"})
        result = copy_draft(Client(TOKEN, BOX, Fake(original, "Текст".encode(), b"file")), MSG, {})
        self.assertEqual(result["DocumentAttachments"][0]["Comment"], "Текст")

    def test_copy_rejects_non_draft_recycled_deleted_transformed(self):
        for changes in ({"IsDraft": False}, {"DraftIsRecycled": True}, {"IsDeleted": True},
                        {"DraftIsTransformedToMessageIdList": [NEW]}, {"FromBoxId": TO}):
            original = source()
            original.update(changes)
            self.error("not_draft", copy_draft, Client(TOKEN, BOX, Fake(original)), MSG, {})

    def test_copy_rejects_unsupported_without_writes(self):
        for mutate in (lambda x: x.update(DraftIsLocked=True),
                       lambda x: x["Entities"].append({"EntityType": "Signature", "ParentEntityId": EID}),
                       lambda x: x["Entities"][0].update(IsEncryptedContent=True),
                       lambda x: x["Entities"][0]["DocumentInfo"].update(InitialDocumentIds=[{"MessageId": MSG, "EntityId": EID}]),
                       lambda x: x["Entities"][0]["DocumentInfo"].update(ParticipantRoles=[{}])):
            original = source()
            mutate(original)
            transport = Fake(original)
            self.error("unsupported_copy", copy_draft, Client(TOKEN, BOX, transport), MSG, {})
            self.assertTrue(all(c[0] == "GET" for c in transport.calls))

    def test_copy_unknown_and_conflicting_entity_ids(self):
        for patch_value in ({"Remove": [NEW]}, {"Remove": [EID], "Replace": {EID: {}}},
                            {"Remove": [EID, EID]}, {"Message": {"DocumentAttachments": []}}):
            self.error("invalid_input", copy_draft, Client(TOKEN, BOX, Fake(source())), MSG, patch_value)

    def test_dry_run_create_without_token_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft.json"
            path.write_text(json.dumps(payload()))
            args = parser().parse_args(["--box-id", BOX, "draft-create", "--input", str(path), "--operation-id", OP, "--dry-run"])
            transport = Fake()
            provider = TokenProvider({}, transport)
            result = run(args, Client(box_id=BOX, transport=transport, token_provider=provider))
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["request"]["DocumentAttachments"][0]["SignedContent"]["Content"], {"base64_characters": 4})
            self.assertFalse(transport.calls)

    def test_dry_run_copy_reads_but_no_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "patch.json"
            path.write_text("{}")
            args = parser().parse_args(["--box-id", BOX, "draft-copy", MSG, "--input", str(path), "--operation-id", OP, "--dry-run"])
            transport = Fake(self.token_response(), source(), b"file")
            provider = TokenProvider(self.credentials(), transport)
            result = run(args, Client(box_id=BOX, transport=transport, token_provider=provider))
            self.assertTrue(result["dry_run"])
            self.assertEqual(transport.calls[0][1], TOKEN_URL)
            self.assertTrue(all(c[0] == "GET" for c in transport.calls[1:]))

    def test_post_draft_operation_id_and_new_id(self):
        transport = Fake({"MessageId": NEW, "IsDraft": True})
        result = Client(TOKEN, BOX, transport).post_draft(prepare(payload(), BOX), OP)
        self.assertEqual(result["MessageId"], NEW)
        self.assertIn("operationId=" + OP, transport.calls[0][1])
        self.assertTrue(transport.calls[0][2]["IsDraft"])

    def test_pending_retry_after_no_automatic_repetition(self):
        for status in (204, 200):
            transport = Fake((status, {"Retry-After": "17"}, b""))
            exc = self.error("operation_pending", Client(TOKEN, BOX, transport).post_draft, prepare(payload(), BOX), OP)
            self.assertEqual(exc.details["retry_after_seconds"], 17)
            self.assertEqual(exc.details["operation_id"], OP)
            self.assertEqual(len(transport.calls), 1)

    def test_unknown_write_results(self):
        for response in (TimeoutError(), (500, {}, b""), b"garbage", {}, {"MessageId": ""}):
            transport = Fake(response)
            exc = self.error("write_outcome_unknown", Client(TOKEN, BOX, transport).post_draft, prepare(payload(), BOX), OP)
            self.assertEqual(exc.details["operation_id"], OP)
            self.assertEqual(len(transport.calls), 1)

    def test_cli_json_errors_and_redaction(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"DIADOC_REFRESH_TOKEN": TOKEN}), patch.object(Client, "organizations", return_value={"note": TOKEN, "access_token": TOKEN}), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["organizations"]), 0)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["note"], "[REDACTED]")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["--invalid", TOKEN]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertEqual(json.loads(err.getvalue())["error"]["code"], "invalid_arguments")

    def test_incomplete_http_body_does_not_expose_data(self):
        transport = Fake(IncompleteRead(TOKEN.encode(), 100))
        exc = self.error("write_outcome_unknown", Client(TOKEN, BOX, transport).post_draft, prepare(payload(), BOX), OP)
        self.assertNotIn(TOKEN, str(exc))
        self.error("network_error", Client(TOKEN, transport=Fake(IncompleteRead(TOKEN.encode()))).organizations)

    def test_preview_checks_total_request_limit(self):
        with patch("diadoc_lib.drafts.MAX_BYTES", 100):
            self.error("invalid_input", prepare, payload(), BOX)

    def test_transport_utf8_timeout_and_response_limit(self):
        response = io.BytesIO(b'{"ok":true}')
        response.status = 200
        response.headers = {}
        transport = HttpTransport(12)
        with patch.object(transport.opener, "open", return_value=response) as opened:
            status, _, _ = transport.request("POST", "https://example.com", {"note": "Текст"}, {"Authorization": "Bearer " + TOKEN})
        request = opened.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"note": "Текст"})
        self.assertEqual(opened.call_args.kwargs["timeout"], 12)
        self.assertEqual(status, 200)
        response = io.BytesIO(b"1234")
        response.status, response.headers = 200, {}
        with patch("diadoc_lib.transport.MAX_BYTES", 3), patch.object(transport.opener, "open", return_value=response):
            self.error("response_too_large", transport.request, "GET", "https://example.com", None, {})
        self.assertTrue(response.closed)

    def test_entrypoint_other_directory_and_stdin_preview(self):
        entry = Path(__file__).resolve().parents[1] / "scripts" / "diadoc.py"
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(entry), "--box-id", BOX, "draft-create", "--input", "-",
                                     "--operation-id", OP, "--dry-run"], input=json.dumps(payload()),
                                    cwd=tmp, text=True, capture_output=True, env={})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["dry_run"])
        self.assertNotIn("YWJj", result.stdout)

    def test_cli_invalid_timeout_and_json_are_structured(self):
        for value in ("nan", "inf", "0", "-1"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(["--timeout", value, "check"]), 1)
            self.assertEqual(json.loads(err.getvalue())["error"]["code"], "invalid_input")
            self.assertFalse(out.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "config.env"
            env_file.write_text("DIADOC_REFRESH_TOKEN=\n")
            path = Path(tmp) / "bad.json"
            path.write_text('{"x":NaN}')
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(["--env-file", str(env_file), "--box-id", BOX, "draft-create", "--input", str(path),
                                       "--operation-id", OP, "--dry-run"]), 1)
            self.assertEqual(json.loads(err.getvalue())["error"]["code"], "invalid_input")

    def test_transport_disallows_redirects_and_closes_http_error(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))
        transport = HttpTransport()
        error = urllib.error.HTTPError("https://example.com", 403, "private", {}, io.BytesIO(TOKEN.encode()))
        with patch.object(transport.opener, "open", side_effect=error):
            status, _, body = transport.request("GET", "https://example.com", None, {})
        self.assertEqual(status, 403)
        self.assertEqual(body, b"")
        self.assertTrue(error.closed)


if __name__ == "__main__":
    unittest.main()
