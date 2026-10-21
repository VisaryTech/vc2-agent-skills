"""Explicitly enabled tests against a disposable 1C test publication; no cleanup writes."""
import os
import unittest

import support  # Adds this skill's scripts directory to sys.path.
from onec_lib.client import Client
from onec_lib.common import load_json
from onec_lib.importer import Importer
from onec_lib.state import State
from onec_lib.transport import Config, Transport


@unittest.skipUnless(os.environ.get("ONEC_ODATA_LIVE_TESTS") == "1", "Live tests disabled; see README")
class LiveTests(unittest.TestCase):
    def test_create_read_and_repeat_without_duplicates(self):
        config = Config()
        state = State(config.state_dir, config.base)
        fixture = load_json(os.environ.get("ONEC_ODATA_LIVE_FIXTURE", ""))
        self.assertTrue({"goods", "services", "upd", "invoice"} <= set(fixture) <= {"goods", "services", "upd", "invoice", "mixed_upd"})
        expected_types = {"goods": "torg12", "services": "act", "upd": "upd", "invoice": "invoice", "mixed_upd": "upd"}
        self.assertEqual(len({v["operation_id"] for v in fixture.values()}), len(fixture))
        for key, data in fixture.items():
            self.assertEqual(data["type"], expected_types[key])
            self.assertTrue(data["number"].startswith("TEST-ONEC-"))
            if "invoice" in data:
                self.assertTrue(data["invoice"]["number"].startswith("TEST-ONEC-"))
        self.assertEqual(fixture["upd"].get("function"), "invoice_transfer")
        self.assertTrue(fixture["goods"]["supplier"]["name"].startswith("TEST-ONEC-"))
        self.assertEqual(fixture["invoice"]["basis"], {"number": fixture["goods"]["number"], "date": fixture["goods"]["date"]})
        self.assertEqual(fixture["invoice"]["supplier"], fixture["goods"]["supplier"])

        class CountingTransport(Transport):
            posts = 0
            def request(self, method, *args, **kwargs):
                if method == "POST":
                    self.posts += 1
                return super().request(method, *args, **kwargs)

        transport = CountingTransport(config, log=state.log)
        client = Client(transport, profile=load_json(state.profile_path))
        importer = Importer(client, state)
        supplier = fixture["goods"]["supplier"]
        found = client.query("counterparties", {"inn": supplier["inn"], "kpp": supplier.get("kpp", "")})
        if found:
            journal = state.read(fixture["goods"]["operation_id"])
            stage = journal["stages"].get("counterparty", {})
            self.assertEqual(stage.get("id"), found[0][client.role("counterparties")["fields"]["id"]],
                             "Для первого запуска нужен отсутствующий тестовый контрагент")
        results = {}
        for key in ["goods", "services", "upd", "invoice"] + (["mixed_upd"] if "mixed_upd" in fixture else []):
            data = fixture[key]
            importer.execute(data, dry_run=True)
            result = importer.execute(data)
            results[key] = result
            for role, identity in [("receipts", result["receipt_id"]), ("invoices", result["invoice_id"])]:
                if identity:
                    row = client.read(role, identity)
                    self.assertIs(row[client.role(role)["fields"]["posted"]], False)
            count = transport.posts
            repeated = importer.execute(data)
            self.assertEqual(count, transport.posts)
            self.assertEqual(result["receipt_id"], repeated["receipt_id"])
            self.assertEqual(result["invoice_id"], repeated["invoice_id"])
        self.assertEqual(results["goods"]["receipt_id"], results["invoice"]["receipt_id"])
        self.assertIsNotNone(results["upd"]["invoice_id"])
        self.assertEqual(state.read(fixture["goods"]["operation_id"])["stages"]["counterparty"]["status"], "verified")


if __name__ == "__main__":
    unittest.main()
