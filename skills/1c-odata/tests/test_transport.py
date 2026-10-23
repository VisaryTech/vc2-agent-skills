"""Isolated metadata and HTTP transport regression tests."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from support import Response
from test_generic import MemoryService, RAW, ENTITY, KEY
from onec_lib.generic import EntityClient
from onec_lib.cli import main
from onec_lib.common import Error, WireNumber, load_json, wire_json
from onec_lib.metadata import parse_metadata, Schema
from onec_lib.transport import Config, NoRedirect, Transport


class MetadataTests(unittest.TestCase):
    def test_namespaces_aliases_and_inheritance(self):
        raw = b'''<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="N" Alias="A">
        <EntityType Name="Base"><Key><PropertyRef Name="ID"/></Key><Property Name="ID" Type="Edm.Guid"/></EntityType>
        <EntityType Name="Child" BaseType="A.Base"><Property Name="Name" Type="Edm.String"/></EntityType>
        <EntityContainer Name="C"><EntitySet Name="Rows" EntityType="A.Child"/></EntityContainer></Schema>'''
        entity = Schema(parse_metadata(raw)).entity("Rows")
        self.assertEqual(entity["keys"], ["ID"])
        self.assertIn("Name", entity["properties"])

    def test_xml_entities_and_malformed_xml_rejected(self):
        for raw in [b"<!DOCTYPE a [<!ENTITY x 'x'>]><a/>", b"<broken>"]:
            with self.assertRaises(Error):
                parse_metadata(raw)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text('{"posted":false,"posted":true}')
            with self.assertRaises(Error):
                load_json(path)

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.config = type("Config", (), {"base": "https://example.invalid/base/odata/", "username": "ai_accountant", "password": "never-print-password"})()
        self.sleeps = []
        self.transport = Transport(self.config, sleep=self.sleeps.append, allow_mutations=True)

    def error(self, status, headers=None):
        return urllib.error.HTTPError(self.config.base, status, "sensitive server error", headers or {}, None)

    def test_get_retries_three_times(self):
        with patch.object(self.transport.opener, "open", side_effect=[self.error(503), self.error(502), OSError(), Response()]) as opened:
            self.assertEqual(self.transport.request("GET", "X"), {"value": []})
        self.assertEqual(self.sleeps, [1, 2, 4])
        self.assertEqual(opened.call_count, 4)

    def test_retry_after_honored(self):
        with patch.object(self.transport.opener, "open", side_effect=[self.error(429, {"Retry-After": "8"}), Response()]):
            self.transport.request("GET", "X")
        self.assertEqual(self.sleeps, [8])

    def test_long_retry_after_returns_without_sleep(self):
        with patch.object(self.transport.opener, "open", side_effect=self.error(429, {"Retry-After": "120"})):
            with self.assertRaises(Error) as caught:
                self.transport.request("GET", "X")
        self.assertEqual(caught.exception.code, "retry_later")
        self.assertEqual(self.sleeps, [])

    def test_post_never_retries(self):
        with patch.object(self.transport.opener, "open", side_effect=self.error(503)) as opened:
            with self.assertRaises(Error) as caught:
                self.transport.request("POST", "X", {})
        self.assertEqual(caught.exception.code, "write_outcome_unknown")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_bad_json_after_post_is_uncertain(self):
        with patch.object(self.transport.opener, "open", return_value=Response(body=b"not JSON")):
            with self.assertRaises(Error) as caught:
                self.transport.request("POST", "X", {})
        self.assertEqual(caught.exception.code, "write_outcome_unknown")

    def test_double_serialized_as_number_without_binary_float(self):
        value = WireNumber("1234567890123.45")
        body = wire_json({"amount": value})
        self.assertIn('"amount":1234567890123.45', body)
        from decimal import Decimal
        self.assertEqual(json.loads(body, parse_float=Decimal)["amount"], Decimal(value))
        with patch.object(self.transport.opener, "open", return_value=Response(body=b'{"amount":1234567890123.45}')) as opened:
            result = self.transport.request("POST", "X", {"amount": value})
        self.assertEqual(opened.call_args[0][0].data.decode(), body)
        self.assertEqual(result["amount"], value)

    def test_filter_spaces_use_percent_encoding_and_results_are_checked(self):
        client = EntityClient(self.transport, parse_metadata(RAW))
        with patch.object(self.transport.opener, "open", return_value=Response(body=b'{"value": [{"F_code":"different"}]}')) as opened:
            with self.assertRaises(Error) as caught:
                client.query_entity(ENTITY, {"Название": "x y"})
        self.assertEqual(caught.exception.code, "invalid_response")
        address = opened.call_args[0][0].full_url
        self.assertIn("%20", address)
        self.assertNotIn("+", address)

    def test_post_redirect_and_timeout_are_uncertain(self):
        for status in [302, 408, 500, 429]:
            with self.subTest(status=status), patch.object(self.transport.opener, "open", side_effect=self.error(status)) as opened:
                with self.assertRaises(Error) as caught:
                    self.transport.request("POST", "X", {})
                self.assertEqual(caught.exception.code, "write_outcome_unknown")
                self.assertEqual(opened.call_count, 1)

    def test_pagination_cannot_leak_credentials(self):
        client = EntityClient(self.transport, parse_metadata(RAW))
        body = json.dumps({"value": [], "odata.nextLink": "https://evil.invalid/base/odata/Set3"}).encode()
        with patch.object(self.transport.opener, "open", return_value=Response(body=body)) as opened:
            with self.assertRaises(Error) as caught:
                client.query_entity(ENTITY, limit=None)
        self.assertEqual(caught.exception.code, "unsafe_url")
        self.assertEqual(opened.call_count, 1)

    def test_all_query_does_not_cap_results_with_top(self):
        service = MemoryService()
        client = service.client
        client.query_entity(ENTITY, limit=None)
        self.assertNotIn("%24top", service.calls[-1][1])
        client.query_entity(ENTITY, limit=3)
        self.assertIn("%24top=3", service.calls[-1][1])

    def test_credentials_not_in_errors(self):
        with patch.object(self.transport.opener, "open", side_effect=self.error(401)):
            with self.assertRaises(Error) as caught:
                self.transport.request("GET", "X")
        self.assertNotIn(self.config.password, json.dumps(caught.exception.result()))
        self.assertNotIn("sensitive", json.dumps(caught.exception.result()))

    def test_unsafe_links_blocked(self):
        for address in ["https://evil.invalid/base/odata/X", "http://example.invalid/base/odata/X", "../outside", "%2e%2e/outside", "/base/odata-other/X", "https://user:password@example.invalid/base/odata/X"]:
            with self.subTest(address=address), self.assertRaises(Error):
                self.transport.safe_url(address)

    def test_forbidden_methods_and_redirects(self):
        for method in ["PUT", "HEAD", "OPTIONS"]:
            with self.assertRaises(Error):
                self.transport.request(method, "X", {})
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
        with patch.object(self.transport.opener, "open", side_effect=self.error(302)) as opened:
            with self.assertRaises(Error):
                self.transport.request("GET", "X")
        self.assertEqual(opened.call_count, 1)

    def test_default_transport_blocks_all_write_methods_before_network(self):
        transport = Transport(self.config)
        with patch.object(transport.opener, "open") as opened:
            for method in ["POST", "PATCH", "DELETE", "PUT"]:
                with self.subTest(method=method), self.assertRaises(Error) as caught:
                    transport.request(method, "X", {})
                self.assertEqual(caught.exception.code, "write_forbidden")
            opened.assert_not_called()

    def test_configuration_and_cli_redact_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"ONEC_ODATA_URL": "http://example.invalid/base/odata", "ONEC_ODATA_USERNAME": "ai_accountant",
                   "ONEC_ODATA_PASSWORD": self.config.password}
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(Error):
                    Config()
                stream = io.StringIO()
                with redirect_stdout(stream):
                    self.assertEqual(main(["check"]), 2)
                self.assertNotIn(self.config.password, stream.getvalue())
                with patch.dict(os.environ, {"ONEC_ODATA_ALLOW_HTTP": "1"}):
                    self.assertEqual(Config().username, "ai_accountant")

    def test_pagination_formats_and_cycle_detection(self):
        service = MemoryService()
        client = service.client
        with patch.object(service, "request", side_effect=[{"d": {"results": [{"F_id": KEY["Область"]}], "__next": "Set3?$skip=1"}}, {"value": [{"F_id": KEY["Область"]}]}]):
            self.assertEqual(len(client.query_entity(ENTITY, limit=None)), 2)
        with patch.object(service, "request", side_effect=[{"value": [], "odata.nextLink": "Set3?loop=1"}, {"value": [], "odata.nextLink": "Set3?loop=1"}]):
            with self.assertRaises(Error):
                client.query_entity(ENTITY, limit=None)
