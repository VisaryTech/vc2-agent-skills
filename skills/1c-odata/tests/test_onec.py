import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from support import Service, IDS, document, fixture
from onec_lib.client import Client, literal
from onec_lib.cli import main
from onec_lib.common import Error, WireNumber, digest, load_json, wire_json
from onec_lib.importer import Importer, validate_input
from onec_lib.metadata import parse_metadata, Schema
from onec_lib.state import State
from onec_lib.transport import Config, NoRedirect, Transport


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service()
        self.client = Client(self.service, profile=self.service.profile)
        self.state = State(self.temp.name, self.service.base)
        self.importer = Importer(self.client, self.state)

    def posts(self):
        return [c for c in self.service.calls if c[0] == "POST"]

    def run_import(self, data=None):
        return self.importer.execute(data or document())

    def test_upd_creates_receipt_and_linked_invoice_unposted(self):
        result = self.run_import()
        self.assertEqual(len(self.posts()), 2)
        receipt = self.service.rows["Set6"][0]
        invoice = self.service.rows["Set7"][0]
        self.assertFalse(receipt["F_posted"])
        self.assertFalse(invoice["F_posted"])
        self.assertEqual(invoice["F_basis"], result["receipt_id"])
        self.assertEqual(receipt["Goods"][0]["R_vat_rate"], "Twenty")
        self.assertEqual(self.state.read(IDS["operation"])["status"], "complete")

    def test_dry_run_makes_no_posts_or_journal(self):
        result = self.importer.execute(document(), dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.posts(), [])
        self.assertFalse(self.state.path(IDS["operation"]).exists())

    def test_operation_uuid_case_does_not_change_journal(self):
        import uuid
        data = document("act")
        data["operation_id"] = str(uuid.uuid4()).upper()
        result = self.run_import(data)
        self.assertEqual(result["operation_id"], data["operation_id"].lower())
        self.assertEqual(self.state.read(result["operation_id"])["status"], "complete")
        data["operation_id"] = data["operation_id"].lower()
        self.run_import(data)
        self.assertEqual(len(self.posts()), 1)

    def test_retry_same_operation_creates_nothing(self):
        first = self.run_import()
        second = self.run_import()
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(len(self.posts()), 2)

    def test_duplicate_different_operation_creates_nothing(self):
        self.run_import()
        data = document()
        data["operation_id"] = IDS["basis"]
        result = self.run_import(data)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(result["stages"]["receipt"]["status"], "existing")

    def test_existing_posted_document_is_never_changed(self):
        data = document("act")
        self.run_import(data)
        self.service.rows["Set6"][0]["F_posted"] = True
        data["operation_id"] = IDS["basis"]
        self.run_import(data)
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(self.service.rows["Set6"][0]["F_posted"])

    def test_own_document_posted_during_resume_blocks(self):
        self.run_import()
        self.service.rows["Set6"][0]["F_posted"] = True
        with self.assertRaisesRegex(Error, "отличаются"):
            self.run_import()
        self.assertEqual(len(self.posts()), 2)

    def test_operation_payload_change_blocks(self):
        self.run_import()
        data = document()
        data["number"] = "changed"
        with self.assertRaises(Error) as caught:
            self.run_import(data)
        self.assertEqual(caught.exception.code, "operation_conflict")

    def test_partial_success_resumes_only_invoice(self):
        self.service.fail_role = "invoices"
        with self.assertRaises(Error):
            self.run_import()
        self.assertEqual(len(self.service.rows["Set6"]), 1)
        self.service.fail_role = None
        self.run_import()
        self.assertEqual(len(self.service.rows["Set6"]), 1)
        self.assertEqual(len(self.service.rows["Set7"]), 1)

    def test_lost_post_response_reconciles_without_resending(self):
        self.service.lose_reply = True
        self.run_import()
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(len(self.service.rows["Set6"]), 1)

    def test_unknown_post_not_found_never_resends(self):
        self.service.invisible_post = True
        for _ in range(2):
            with self.assertRaises(Error) as caught:
                self.run_import()
            self.assertEqual(caught.exception.code, "write_outcome_unknown")
        self.assertEqual(len(self.posts()), 1)

    def test_returned_id_persisted_before_read_failure(self):
        self.service.fail_read = True
        with self.assertRaises(Error):
            self.run_import()
        stage = self.state.read(IDS["operation"])["stages"]["receipt"]
        self.assertEqual(stage["status"], "created_unverified")
        self.assertTrue(stage["id"])
        self.service.fail_read = False
        self.run_import()
        self.assertEqual(len(self.posts()), 2)

    def test_conflicting_duplicate_blocks(self):
        self.run_import()
        self.service.rows["Set6"][0]["F_total"] = "121.00"
        with self.assertRaises(Error) as caught:
            self.run_import()
        self.assertEqual(caught.exception.code, "duplicate_conflict")
        self.assertEqual(len(self.posts()), 2)

    def test_missing_supplier_created_once(self):
        self.service.rows["Set1"] = []
        data = document("act")
        data.pop("contract")
        self.run_import(data)
        self.run_import(data)
        self.assertEqual(len(self.service.rows["Set1"]), 1)
        self.assertEqual(len(self.posts()), 2)

    def test_missing_supplier_dry_run_only_plans(self):
        self.service.rows["Set1"] = []
        data = document("act")
        data.pop("contract")
        result = self.importer.execute(data, True)
        self.assertTrue(result["pending_supplier"])
        self.assertEqual(result["counterparty_create"]["F_name"], "TEST supplier")
        self.assertEqual(self.posts(), [])

    def test_ambiguous_supplier_blocks(self):
        self.service.rows["Set1"] *= 2
        with self.assertRaises(Error) as caught:
            self.run_import()
        self.assertEqual(caught.exception.code, "ambiguous_reference")
        self.assertEqual(self.posts(), [])

    def test_missing_item_blocks_before_supplier_creation(self):
        self.service.rows["Set1"] = []
        self.service.rows["Set3"] = []
        data = document("act")
        data.pop("contract")
        with self.assertRaises(Error):
            self.run_import(data)
        self.assertEqual(self.posts(), [])

    def test_wrong_contract_owner_blocks(self):
        self.service.rows["Set2"][0]["F_supplier"] = IDS["item"]
        with self.assertRaises(Error) as caught:
            self.run_import()
        self.assertEqual(caught.exception.code, "reference_conflict")
        self.assertEqual(self.posts(), [])

    def test_invoice_auto_basis_lookup(self):
        self.run_import(document("torg12"))
        data = document("invoice")
        data["operation_id"] = IDS["basis"]
        result = self.run_import(data)
        self.assertEqual(result["receipt_id"], self.service.rows["Set6"][0]["F_id"])
        self.assertEqual(len(self.posts()), 2)

    def test_invoice_explicit_basis_amount_checked(self):
        result = self.run_import(document("act"))
        data = document("invoice")
        data["operation_id"] = IDS["basis"]
        data["basis"] = {"id": result["receipt_id"]}
        data["total"] = "121.00"
        with self.assertRaises(Error):
            self.run_import(data)
        self.assertEqual(len(self.posts()), 1)

    def test_amount_error_prevents_requests(self):
        data = document()
        data["lines"][0]["vat"] = "19.99"
        before = len(self.service.calls)
        with self.assertRaises(Error):
            self.run_import(data)
        self.assertEqual(len(self.service.calls), before)

    def test_zero_and_no_vat_remain_distinct(self):
        for rate, wire in [("0", "Zero"), ("none", "NoTax")]:
            data = document("act")
            data["lines"][0].update(net="120.00", vat="0.00", vat_rate=rate)
            data["vat_total"] = "0.00"
            result = self.importer.execute(data, True)
            self.assertEqual(result["receipt"]["Services"][0]["R_vat_rate"], wire)

    def test_net_price_calculation(self):
        data = document("act")
        data["includes_vat"] = False
        data["lines"][0]["price"] = "50.00"
        result = self.importer.execute(data, True)
        self.assertEqual(result["receipt"]["Services"][0]["R_amount"], "100.00")

    def test_unknown_rate_and_non_string_amount_rejected(self):
        for change in [("vat_rate", "99"), ("price", 60.0)]:
            data = document()
            data["lines"][0][change[0]] = change[1]
            with self.assertRaises(Error):
                self.run_import(data)
        self.assertEqual(self.posts(), [])

    def test_upd_requires_explicit_function(self):
        data = document()
        data.pop("function")
        with self.assertRaises(Error):
            self.run_import(data)

    def test_lock_blocks_concurrent_import(self):
        with self.state.lock():
            with self.assertRaises(Error) as caught:
                self.run_import()
        self.assertEqual(caught.exception.code, "operation_locked")

    def test_write_methods_and_roles_restricted(self):
        for role in ["organizations", "items", "contracts", "units"]:
            with self.assertRaises(Error):
                self.client.create(role, {})
        body = self.importer.execute(document(), True)["receipt"]
        body["F_posted"] = True
        with self.assertRaises(Error):
            self.client.create("receipts", body)
        self.assertEqual(self.posts(), [])

    def test_metadata_mismatch_missing_field_and_wrong_type(self):
        for mutate in [lambda p: p.update(metadata_sha256="changed"),
                       lambda p: p["roles"]["receipts"]["fields"].update(number="Unknown"),
                       lambda p: p["roles"]["receipts"]["fields"].update(posted="F_number")]:
            profile = copy.deepcopy(self.service.profile)
            mutate(profile)
            with self.assertRaises(Error):
                Client(self.service, profile=profile)

    def test_mandatory_creation_data_checked_before_post(self):
        self.service.profile["roles"]["receipts"]["required_on_create"] = ["F_contract"]
        data = document("act")
        data.pop("contract")
        self.service.rows["Set1"] = []
        with self.assertRaises(Error) as caught:
            self.run_import(data)
        self.assertEqual(caught.exception.code, "missing_data")
        self.assertEqual(self.posts(), [])

    def test_unmapped_contract_not_silently_dropped(self):
        self.service.profile["roles"]["receipts"]["fields"].pop("contract")
        with self.assertRaises(Error) as caught:
            self.run_import()
        self.assertEqual(caught.exception.code, "invalid_profile")
        self.assertEqual(self.posts(), [])

    def test_invoice_basis_table(self):
        profile = self.service.profile
        profile["roles"]["invoices"]["fields"].pop("basis")
        profile["roles"]["invoices"]["basis_table"] = {
            "property": "BasisRows", "fields": {"basis": "Source", "line_no": "Position"}}
        self.service.metadata["types"]["Synthetic.Type7"]["properties"]["BasisRows"] = {
            "Name": "BasisRows", "Type": "Collection(Synthetic.BasisRow)"}
        self.service.metadata["types"]["Synthetic.BasisRow"] = {"kind": "ComplexType", "base": None, "keys": [], "members": [], "navigation": {},
            "properties": {"Source": {"Type": "Edm.String"}, "Position": {"Type": "Edm.Int32"}}}
        client = Client(self.service, self.service.metadata, profile)
        importer = Importer(client, self.state)
        result = importer.execute(document())
        self.assertEqual(self.service.rows["Set7"][0]["BasisRows"], [{"Source": result["receipt_id"], "Position": 1}])
        importer.execute(document())
        self.assertEqual(len(self.posts()), 2)

    def test_basis_without_header_vat_sums_rows(self):
        self.run_import(document("act"))
        self.service.profile["roles"]["receipts"]["fields"].pop("vat_total")
        data = document("invoice")
        data["operation_id"] = IDS["basis"]
        result = self.run_import(data)
        self.assertTrue(result["invoice_id"])

    def test_double_fields_and_services_without_unit_property(self):
        metadata = self.service.metadata
        for typ in metadata["types"].values():
            for prop in typ["properties"].values():
                if prop["Type"] == "Edm.Decimal":
                    prop["Type"] = "Edm.Double"
        metadata["types"]["Synthetic.ServiceRow"] = copy.deepcopy(metadata["types"]["Synthetic.Row"])
        metadata["types"]["Synthetic.ServiceRow"]["properties"].pop("R_unit")
        metadata["types"]["Synthetic.Type6"]["properties"]["Services"]["Type"] = "Collection(Synthetic.ServiceRow)"
        self.service.profile["roles"]["receipts"]["tables"]["services"]["fields"].pop("unit")
        client = Client(self.service, metadata, self.service.profile)
        importer = Importer(client, self.state)
        result = importer.execute(document("act"))
        body = self.posts()[0][2]
        self.assertIsInstance(body["F_total"], WireNumber)
        self.assertNotIn("R_unit", body["Services"][0])
        wire = json.loads(wire_json(body))
        self.assertEqual(wire["F_total"], 120)
        self.assertEqual(result["receipt_id"], importer.execute(document("act"))["receipt_id"])

    def test_server_empty_guid_and_int64_string_normalization(self):
        data = document("act")
        data.pop("contract")
        self.run_import(data)
        row = self.service.rows["Set6"][0]
        row["F_contract"] = "00000000-0000-0000-0000-000000000000"
        row["Services"][0]["R_line_no"] = "1"
        self.run_import(data)
        self.assertEqual(len(self.posts()), 1)

    def test_zero_and_no_tax_cannot_map_to_same_value(self):
        self.service.profile["vat_rates"]["none"] = self.service.profile["vat_rates"]["0"]
        with self.assertRaises(Error):
            Client(self.service, self.service.metadata, self.service.profile)

    def test_creation_disabled_in_metadata(self):
        self.service.metadata["entity_sets"]["Set6"]["attributes"]["{urn:example}creatable"] = "false"
        with self.assertRaises(Error):
            Client(self.service, self.service.metadata, self.service.profile)

    def test_extra_lines_in_duplicate_blocked(self):
        self.run_import()
        self.service.rows["Set6"][0]["Services"] = copy.deepcopy(self.service.rows["Set6"][0]["Goods"])
        with self.assertRaises(Error) as caught:
            self.run_import()
        self.assertEqual(caught.exception.code, "duplicate_conflict")

    def test_supplier_uncertain_resume_verifies_original_payload(self):
        self.service.rows["Set1"] = []
        data = document("act")
        data.pop("contract")
        self.service.lose_reply = True
        self.service.fail_role = "receipts"
        with self.assertRaises(Error):
            self.run_import(data)
        self.service.rows["Set1"][0]["F_name"] = "modified"
        self.service.fail_role = None
        with self.assertRaises(Error) as caught:
            self.run_import(data)
        self.assertEqual(caught.exception.code, "duplicate_conflict")

    def test_filter_quotes_escaped(self):
        self.assertEqual(literal("O'Brien", "Edm.String"), "'O''Brien'")
        self.service.rows["Set3"][0]["F_code"] = "O'Brien"
        self.assertEqual(len(self.client.query("items", {"code": "O'Brien"})), 1)

    def test_state_files_private(self):
        self.run_import()
        self.assertEqual(self.state.path(IDS["operation"]).stat().st_mode & 0o777, 0o600)
        log = (self.state.directory / "operations.jsonl").read_text()
        self.assertNotIn("TEST supplier", log)
        self.assertNotIn("1234567890", log)


class CliTests(unittest.TestCase):
    def test_cli_discover_import_and_operation_status(self):
        service = Service()
        with tempfile.TemporaryDirectory() as tmp:
            env = {"ONEC_ODATA_URL": service.base, "ONEC_ODATA_USERNAME": "ai_accountant",
                   "ONEC_ODATA_PASSWORD": "private-password", "ONEC_ODATA_STATE_DIR": tmp}
            with patch.dict(os.environ, env, clear=True), patch("onec_lib.cli.Transport", return_value=service):
                def invoke(argv):
                    stream = io.StringIO()
                    with redirect_stdout(stream):
                        code = main(argv)
                    self.assertEqual(code, 0, stream.getvalue())
                    return json.loads(stream.getvalue())
                self.assertEqual(invoke(["check"])["write_access"], "not_tested")
                discovery = invoke(["discover"])
                template = load_json(discovery["template_path"])
                self.assertFalse(template["reviewed"])
                self.assertEqual(template["roles"]["receipts"]["entity_set"], "")
                profile_path = Path(discovery["profile_path"])
                profile_path.write_text(json.dumps(service.profile))
                input_path = Path(tmp) / "input.json"
                input_path.write_text(json.dumps(document()))
                self.assertTrue(invoke(["import", str(input_path), "--dry-run"])["dry_run"])
                self.assertEqual(len([c for c in service.calls if c[0] == "POST"]), 0)
                self.assertTrue(invoke(["import", str(input_path)])["receipt_id"])
                status = invoke(["operation-status", IDS["operation"]])
                self.assertEqual(status["operation"]["status"], "complete")
                self.assertEqual(len(invoke(["list", "receipts", "--all"])["items"]), 1)
                self.assertFalse(invoke(["find-counterparty", "--inn", "1234567890", "--kpp", "123456789"])["ambiguous"])


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


class Response:
    def __init__(self, status=200, body=b'{"value": []}', headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, _):
        return self.body


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.config = type("Config", (), {"base": "https://example.invalid/base/odata/", "username": "ai_accountant", "password": "never-print-password"})()
        self.sleeps = []
        self.transport = Transport(self.config, sleep=self.sleeps.append)

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
        raw, metadata, profile = fixture()
        client = Client(self.transport, metadata, profile)
        with patch.object(self.transport.opener, "open", return_value=Response(body=b'{"value": [{"F_code":"different"}]}')) as opened:
            with self.assertRaises(Error) as caught:
                client.query("items", {"code": "x y"})
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
        raw, metadata, profile = fixture()
        client = Client(self.transport, metadata, profile)
        body = json.dumps({"value": [], "odata.nextLink": "https://evil.invalid/base/odata/Set3"}).encode()
        with patch.object(self.transport.opener, "open", return_value=Response(body=body)) as opened:
            with self.assertRaises(Error) as caught:
                client.query("items")
        self.assertEqual(caught.exception.code, "unsafe_url")
        self.assertEqual(opened.call_count, 1)

    def test_all_query_does_not_cap_results_with_top(self):
        raw, metadata, profile = fixture()
        service = Service()
        client = Client(service, metadata, profile)
        client.query("items")
        self.assertNotIn("%24top", service.calls[-1][1])
        client.query("items", limit=3)
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
        for method in ["PUT", "PATCH", "DELETE"]:
            with self.assertRaises(Error):
                self.transport.request(method, "X", {})
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
        with patch.object(self.transport.opener, "open", side_effect=self.error(302)) as opened:
            with self.assertRaises(Error):
                self.transport.request("GET", "X")
        self.assertEqual(opened.call_count, 1)

    def test_configuration_and_cli_redact_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"ONEC_ODATA_URL": "http://example.invalid/base/odata", "ONEC_ODATA_USERNAME": "ai_accountant",
                   "ONEC_ODATA_PASSWORD": self.config.password, "ONEC_ODATA_STATE_DIR": tmp}
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
        raw, metadata, profile = fixture()
        service = Service()
        client = Client(service, metadata, profile)
        with patch.object(service, "request", side_effect=[{"d": {"results": [{"F_id": IDS["item"]}], "__next": "Set3?$skip=1"}}, {"value": [{"F_id": IDS["unit"]}]}]):
            self.assertEqual(len(client.query("items")), 2)
        with patch.object(service, "request", side_effect=[{"value": [], "odata.nextLink": "Set3?loop=1"}, {"value": [], "odata.nextLink": "Set3?loop=1"}]):
            with self.assertRaises(Error):
                client.query("items")


if __name__ == "__main__":
    unittest.main()
