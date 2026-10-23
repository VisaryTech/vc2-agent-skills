"""Version preconditions, redacted HTTP errors and uncertain write regressions."""
import json
import os
import unittest
from unittest.mock import patch
import urllib.error

from support import Response
from test_generic import ENTITY, KEY, MemoryService
from onec_lib.common import Error
from onec_lib.cli import parser, run
from onec_lib.generic import Mutations
from onec_lib.transport import Transport


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.service = MemoryService()
        self.mutations = Mutations(self.service.client)

    def test_precedence_and_actual_write_headers(self):
        for explicit, etag, data_version, expected in [
                ("explicit", 'W/"etag"', "dv", "explicit"),
                (None, 'W/"etag"', "dv", 'W/"etag"'), (None, None, "dv", "dv"),
                (None, None, None, None)]:
            with self.subTest(expected=expected):
                self.service.seed(ENTITY, {**KEY, "Название": "old", **({"DataVersion": data_version} if data_version else {})})
                self.service.response_headers = {"eTaG": etag} if etag else {}
                preview = self.mutations.execute(ENTITY, "update", {"Название": "new"}, KEY, True, explicit)
                self.assertEqual(preview["concurrency_protected"], expected is not None)
                self.assertEqual(preview["headers"], {"If-Match": expected} if expected else {})
                self.service.write_headers.clear()
                result = self.mutations.execute(ENTITY, "update", {"Название": "new"}, KEY, if_match=explicit)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(self.service.write_headers, [{"If-Match": expected}] if expected else [])

    def test_mark_deletion_and_delete_forward_version(self):
        for op, data in [("mark-deletion", {"DeletionMark": True}), ("delete", None)]:
            self.service.seed(ENTITY, {**KEY, "DataVersion": "v1"})
            self.service.write_headers.clear()
            self.mutations.execute(ENTITY, op, data, KEY)
            self.assertEqual(self.service.write_headers, [{"If-Match": "v1"}])

    def test_concurrent_update_preserves_other_session_change_and_never_retries(self):
        self.service.seed(ENTITY, {**KEY, "Название": "old", "DataVersion": "v1"})
        original = self.service.request
        writes = []
        def publication(method, address, payload=None, **kwargs):
            if method == "PATCH":
                writes.append(kwargs)
                # Another session changed the object after our preliminary GET.
                self.service.seed(ENTITY, {**KEY, "Название": "other session", "DataVersion": "v2"})
                if kwargs.get("headers", {}).get("If-Match") != "v2":
                    raise Error("concurrency_conflict", "Conflict", http_status=412)
            return original(method, address, payload, **kwargs)
        with patch.object(self.service, "request", side_effect=publication), self.assertRaises(Error) as caught:
            self.mutations.execute(ENTITY, "update", {"Название": "mine"}, KEY)
        self.assertEqual(caught.exception.code, "concurrency_conflict")
        self.assertEqual(caught.exception.details, {"http_status": 412, "key": KEY, "entity_set": ENTITY, "operation": "update"})
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.service.client.get(ENTITY, KEY)["Название"], "other session")

    def test_invalid_version_never_writes_and_wildcard_is_not_protection(self):
        for version in ["", "x\r\nAuthorization: secret", "Я", 42]:
            with self.subTest(version=version), self.assertRaises(Error):
                self.mutations.execute(ENTITY, "update", {"Название": "new"}, KEY, if_match=version)
        self.assertFalse(any(call[0] != "GET" for call in self.service.calls))
        preview = self.mutations.execute(ENTITY, "delete", key=KEY, dry_run=True, if_match="*")
        self.assertFalse(preview["concurrency_protected"])
        with self.assertRaises(Error):
            self.mutations.execute(ENTITY, "create", KEY, if_match="v1")

    def test_if_match_does_not_override_posted_document_guard(self):
        self.service.seed("Document_Тест", {**self.service.doc_key, "Posted": True, "DataVersion": "v1"})
        for op, data in [("update", {"Number": "2"}), ("mark-deletion", {"DeletionMark": True}), ("delete", None)]:
            with self.subTest(op=op), self.assertRaises(Error) as caught:
                self.mutations.execute("Document_Тест", op, data, self.service.doc_key, if_match="*")
            self.assertEqual(caught.exception.code, "write_forbidden")
        self.assertFalse(any(call[0] != "GET" for call in self.service.calls))

    def test_cli_explicit_version_reaches_patch(self):
        env = {"ONEC_ODATA_URL": self.service.base, "ONEC_ODATA_USERNAME": "u", "ONEC_ODATA_PASSWORD": "p"}
        with patch.dict(os.environ, env, clear=True), patch("onec_lib.cli.Transport", return_value=self.service):
            result = run(parser().parse_args(["mark-deletion", ENTITY, "--key", json.dumps(KEY), "--if-match", "old-version"]))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.service.write_headers, [{"If-Match": "old-version"}])


class TransportVersionTests(unittest.TestCase):
    def setUp(self):
        self.config = type("Config", (), {"base": "https://example.invalid/base/odata/", "username": "u", "password": "secret"})()
        self.transport = Transport(self.config, allow_mutations=True)

    def test_headers_are_opt_in_and_only_if_match_is_allowed(self):
        with patch.object(self.transport.opener, "open", return_value=Response(body=b'{"DataVersion":"v1"}', headers={"ETag": '"e1"'})):
            self.assertEqual(self.transport.request("GET", "X"), {"DataVersion": "v1"})
            self.assertEqual(self.transport.request("GET", "X", with_headers=True), ({"DataVersion": "v1"}, {"ETag": '"e1"'}))
        with patch.object(self.transport.opener, "open", return_value=Response(status=204, body=b"")) as request:
            self.transport.request("PATCH", "X", {}, headers={"If-Match": "v1"})
        self.assertEqual(dict(request.call_args.args[0].header_items())["If-match"], "v1")
        for method, headers in [("GET", {"If-Match": "v1"}), ("PATCH", {"Authorization": "evil"}),
                                ("PATCH", {"If-Match": "v1\nX: y"})]:
            with self.subTest(headers=headers), patch.object(self.transport.opener, "open") as request, self.assertRaises(Error):
                self.transport.request(method, "X", headers=headers)
            request.assert_not_called()

    def test_known_http_errors_are_definite_and_redacted_without_retry(self):
        codes = {401: "authentication_error", 403: "access_denied", 404: "object_not_found",
                 405: "operation_not_supported", 412: "concurrency_conflict"}
        for method in ["GET", "PATCH", "DELETE", "POST"]:
            for status, code in codes.items():
                error = urllib.error.HTTPError(self.config.base, status, "secret server body", {}, None)
                with self.subTest(method=method, status=status), patch.object(self.transport.opener, "open", side_effect=error) as request, self.assertRaises(Error) as caught:
                    self.transport.request(method, "X")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.details["http_status"], status)
                self.assertNotIn("secret", json.dumps(caught.exception.result()))
                self.assertEqual(request.call_count, 1)

    def test_definite_404_after_delete_confirms_completion(self):
        service = MemoryService()
        service.client.transport = self.transport
        row = json.dumps(KEY).encode()
        not_found = urllib.error.HTTPError(self.config.base, 404, "secret", {}, None)
        with patch.object(self.transport.opener, "open", side_effect=[Response(body=row), Response(status=204, body=b""), not_found]) as request:
            result = Mutations(service.client).execute(ENTITY, "delete", key=KEY)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([call.args[0].method for call in request.call_args_list], ["GET", "DELETE", "GET"])
