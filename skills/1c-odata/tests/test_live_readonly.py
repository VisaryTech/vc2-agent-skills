"""Opt-in live smoke test. Remote requests are restricted to GET."""
import os
import unittest

import support  # Adds the skill's scripts directory to sys.path.
from onec_lib.generic import EntityClient
from onec_lib.transport import Config, Transport


class ReadOnlyTransport(Transport):
    def request(self, method, *args, **kwargs):
        if method != "GET":
            raise AssertionError("Live verification permits GET only")
        return super().request(method, *args, **kwargs)


@unittest.skipUnless(os.environ.get("ONEC_ODATA_LIVE_READ_TESTS") == "1", "Live read tests disabled")
class LiveReadTests(unittest.TestCase):
    def test_metadata_query_and_get(self):
        config = Config()
        client = EntityClient(ReadOnlyTransport(config))
        self.assertTrue(client.entities())
        entity = os.environ.get("ONEC_ODATA_LIVE_ENTITY")
        if not entity:
            entity = next(name for name in sorted(client.schema.data["entity_sets"])
                          if client.schema.entity(name)["keys"])
        schema = client.describe(entity)
        self.assertTrue(schema["keys"])
        rows = client.query_entity(entity, select=schema["keys"], limit=2)
        for key in rows:
            record = client.get(entity, key)
            self.assertTrue(client.matches(schema, record, key))
