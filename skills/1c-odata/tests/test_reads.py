"""Observable query behavior against a simulated OData publication."""
import copy
import json
import os
import unittest
from unittest.mock import patch
import urllib.parse as url

from support import IDS
from test_generic import RAW, ENTITY, KEY, MemoryService
from onec_lib.cli import parser, run
from onec_lib.common import Error
from onec_lib.generic import EntityClient
from onec_lib.metadata import parse_metadata


def comparison(op, field, value):
    return {"op": op, "field": field, "value": value}


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.service = MemoryService()
        self.client = self.service.client

    def params(self):
        return url.parse_qs(url.urlsplit(self.service.calls[-1][1]).query)

    def test_nested_filter_dates_guid_precision_and_projection(self):
        tree = {"op": "and", "args": [
            comparison("ge", "Дата", "2026-10-01"), comparison("lt", "Дата", "2026-11-01"),
            comparison("gt", "Измерение", "1234567890123.123456"), comparison("eq", "Ссылка", IDS["item"]),
            {"op": "or", "args": [comparison("contains", "Название", "O'Brien"),
                                      {"op": "not", "args": [comparison("eq", "DeletionMark", True)]}]}]}
        row = {**KEY, "Дата": "2026-10-23T10:00:00", "Измерение": "1234567890123.123457",
               "Ссылка": IDS["item"], "Название": "O'Brien", "DeletionMark": False}
        with patch.object(self.service, "request", return_value={"value": [row]}) as request:
            self.assertEqual(self.client.query_entity(ENTITY, KEY, select=["Название"], filter_tree=tree),
                             [{"Название": "O'Brien"}])
        query = url.parse_qs(url.urlsplit(request.call_args.args[1]).query)
        self.assertIn("substringof('O''Brien',Название)", query["$filter"][0])
        self.assertIn("guid'" + IDS["item"] + "'", query["$filter"][0])
        self.assertIn("1234567890123.123456D", query["$filter"][0])
        self.assertIn("datetime'2026-10-01T00:00:00'", query["$filter"][0])
        self.assertIn("Дата", query["$select"][0])

    def test_server_ignoring_filter_is_rejected(self):
        for tree in [comparison("gt", "Номер", 2**63-2), comparison("eq", "Название", "Исходное")]:
            with self.subTest(tree=tree):
                self.assertEqual(len(self.client.query_entity(ENTITY, filter_tree=tree)), 1)
        for tree in [comparison("lt", "Номер", 10), comparison("startswith", "Название", "Нет"),
                     comparison("endswith", "Название", "Нет"), comparison("ne", "Название", "Исходное")]:
            with self.subTest(tree=tree), self.assertRaises(Error) as caught:
                self.client.query_entity(ENTITY, filter_tree=tree)
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_date_response_and_numeric_order_are_typed(self):
        for row, tree in [({"Дата": "/Date(0)/"}, comparison("ge", "Дата", "1970-01-01")),
                          ({"Номер": "10"}, comparison("gt", "Номер", "2")),
                          ({"Момент": "2026-10-23T12:00:00+03:00"},
                           comparison("le", "Момент", "2026-10-23T09:00:00Z"))]:
            with self.subTest(row=row), patch.object(self.service, "request", return_value={"value": [row]}):
                self.assertEqual(self.client.query_entity(ENTITY, filter_tree=tree), [row])

    def test_invalid_filters_fail_before_query(self):
        invalid = [comparison("eq", "Missing", "x"), comparison("gt", "DeletionMark", False),
                   comparison("contains", "Номер", "2"), comparison("eq", "Ссылка", "bad"),
                   comparison("gt", "Дата", "2026-02-30"), comparison("gt", "Сумма", "0.001"),
                   comparison("eq", "Строки", []), comparison("eq", "Строки", None), {"op": "or", "args": []},
                   {"op": "not", "args": []}, {"op": "and", "args": {}},
                   {"op": "eq", "field": "Название", "value": "x", "raw": "evil"}]
        for tree in invalid:
            with self.subTest(tree=tree), patch.object(self.service, "request") as request, self.assertRaises(Error):
                self.client.query_entity(ENTITY, filter_tree=tree)
            request.assert_not_called()

    def test_filter_requires_all_fields_even_in_unused_or_branch(self):
        tree = {"op": "or", "args": [comparison("eq", "Название", "x"), comparison("gt", "Номер", 1)]}
        with patch.object(self.service, "request", return_value={"value": [{"Название": "x"}]}), self.assertRaises(Error):
            self.client.query_entity(ENTITY, filter_tree=tree)

    def test_boolean_operators_and_null_equality(self):
        for value, op, expected in [(None, "eq", True), (None, "ne", False), ("x", "ne", True)]:
            with self.subTest(value=value, op=op), patch.object(self.service, "request", return_value={"value": [{"Название": value}]}):
                if expected:
                    self.assertEqual(self.client.query_entity(ENTITY, filter_tree=comparison(op, "Название", None)), [{"Название": value}])
                else:
                    with self.assertRaises(Error):
                        self.client.query_entity(ENTITY, filter_tree=comparison(op, "Название", None))
        tree = {"op": "not", "args": [{"op": "or", "args": [comparison("eq", "Название", "x"),
                                                                    comparison("eq", "Название", "y")]}]}
        self.assertEqual(len(self.client.query_entity(ENTITY, filter_tree=tree)), 1)

    def test_query_record_and_page_limits_are_preserved(self):
        with patch.object(self.service, "request", return_value={"value": [{}] * 100001}), self.assertRaises(Error) as caught:
            self.client.query_entity(ENTITY, limit=None)
        self.assertEqual(caught.exception.code, "query_too_large")
        page_number = 0
        def advancing_pages(method, address):
            nonlocal page_number
            page_number += 1
            return {"value": [], "odata.nextLink": "?page=" + str(page_number)}
        with patch.object(self.service, "request", side_effect=advancing_pages) as request, self.assertRaises(Error) as caught:
            self.client.query_entity(ENTITY, limit=None)
        self.assertEqual(request.call_count, 10000)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_presentations_and_navigation_are_returned_with_projection(self):
        raw = RAW.replace(b'</Schema>', b'''<Association Name="Owner"><End Role="Object" Type="D.Object" Multiplicity="*"/>
        <End Role="Owner" Type="D.Object" Multiplicity="0..1"/></Association></Schema>''')
        client = EntityClient(self.service, parse_metadata(raw))
        row = {**KEY, "Название": "x", "Ссылка____Presentation": "Partner", "Owner": {"Название": "Owner"}}
        with patch.object(self.service, "request", return_value={"value": [row]}) as request:
            result = client.query_entity(ENTITY, select=["Название"], presentations=True, expand=["Owner"])
        self.assertEqual(result, [{k: v for k, v in row.items() if k not in KEY}])
        query = url.parse_qs(url.urlsplit(request.call_args.args[1]).query)
        self.assertEqual(query["$expand"], ["Owner"])
        self.assertIn("*____Presentation", query["$select"][0])
        for name in ["Missing", "Owner/Owner", "Owner($expand=Owner)"]:
            with self.subTest(name=name), self.assertRaises(Error):
                client.query_entity(ENTITY, expand=[name])
        with self.assertRaises(Error):
            self.client.query_entity(ENTITY, expand=["Owner"])
        with self.assertRaises(Error):
            client.query_entity(ENTITY, select=["Missing____Presentation"])
        with patch.object(self.service, "request", return_value={"value": [row]}):
            self.assertEqual(client.query_entity(ENTITY, select=["Ссылка____Presentation"]),
                             [{"Ссылка____Presentation": "Partner"}])
        with self.assertRaises(Error):
            client.encode(client.schema.entity(ENTITY), {"Ссылка____Presentation": "x"})

    def test_count_response_shapes_and_filters(self):
        for body in [{"odata.count": 23}, {"@odata.count": "23"}, {"d": {"__count": "23"}}]:
            with self.subTest(body=body), patch.object(self.service, "request", return_value=body) as request:
                self.assertEqual(self.client.count(ENTITY, {"Название": "x"}), 23)
            query = url.parse_qs(url.urlsplit(request.call_args.args[1]).query)
            self.assertEqual(query["$top"], ["0"])
            self.assertEqual(query["$inlinecount"], ["allpages"])
            self.assertIn("Название eq 'x'", query["$filter"][0])
        for count in [-1, True, 1.5, "-1", "1.5", "9" * 100, str(2**63)]:
            with patch.object(self.service, "request", return_value={"odata.count": count}), self.assertRaises(Error):
                self.client.count(ENTITY)
        with patch.object(self.service, "request", return_value={"value": []}), self.assertRaises(Error) as caught:
            self.client.count(ENTITY)
        self.assertEqual(caught.exception.code, "operation_not_supported")

    def test_skip_pages_use_actual_page_size_and_total_order(self):
        pages = [{"value": [{**KEY, "Номер": str(i)}]} for i in (1, 2)] + [{"value": []}]
        with patch.object(self.service, "request", side_effect=pages) as request:
            rows = self.client.query_entity(ENTITY, order_by=["Номер desc"], limit=None, pagination="skip", page_size=2)
        self.assertEqual([row["Номер"] for row in rows], ["1", "2"])
        queries = [url.parse_qs(url.urlsplit(call.args[1]).query) for call in request.call_args_list]
        self.assertEqual([q["$skip"][0] for q in queries], ["0", "1", "2"])
        self.assertEqual(queries[0]["$orderby"], ["Номер desc,Область asc"])
        self.assertEqual(queries[0]["$top"], ["2"])

    def test_skip_requires_all_keys_and_valid_size_and_detects_links(self):
        for kwargs in [{"pagination": "skip"}, {"page_size": 0}, {"page_size": 10001}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(Error):
                self.client.query_entity(ENTITY, **kwargs)
        metadata = copy.deepcopy(self.service.metadata)
        metadata["types"]["Demo.Object"]["keys"] = []
        with self.assertRaises(Error):
            EntityClient(self.service, metadata).query_entity(ENTITY, limit=None, pagination="skip")
        for link, expected in [("?page=2", "operation_not_supported"), ("https://evil.invalid/", "unsafe_url")]:
            with patch.object(self.service, "request", return_value={"value": [], "odata.nextLink": link}), self.assertRaises(Error) as caught:
                self.client.query_entity(ENTITY, limit=None, pagination="skip")
            self.assertEqual(caught.exception.code, expected)

    def test_new_cli_reads_have_no_filesystem_or_write_access(self):
        env = {"ONEC_ODATA_URL": self.service.base, "ONEC_ODATA_USERNAME": "u", "ONEC_ODATA_PASSWORD": "p"}
        with patch.dict(os.environ, env, clear=True), patch("onec_lib.cli.Transport", return_value=self.service) as factory, \
                patch("builtins.open", side_effect=AssertionError("Unexpected file access")):
            result = run(parser().parse_args(["query", ENTITY, "--filter", json.dumps(comparison("eq", "Название", "Исходное")),
                                               "--presentations"]))
            self.assertTrue(result["ok"])
            self.assertFalse(factory.call_args.kwargs["allow_mutations"])
            with patch.object(self.service, "request", side_effect=[RAW, {"__count": "1"}]):
                self.assertEqual(run(parser().parse_args(["count", ENTITY])), {"ok": True, "count": 1})
            self.assertFalse(factory.call_args.kwargs["allow_mutations"])


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.service = MemoryService()
        self.info = "InformationRegister_Rates"
        self.accum = "AccumulationRegister_Stock"
        schema = '''<Schema Namespace="N" Alias="A" xmlns:m="urn:metadata">
        <EntityType Name="Record"><Key><PropertyRef Name="Currency"/></Key>
        <Property Name="Currency" Type="Edm.Guid"/><Property Name="Warehouse" Type="Edm.String"/></EntityType>
        <ComplexType Name="Result"><Property Name="Amount" Type="Edm.Decimal" Precision="20" Scale="2"/></ComplexType>
        <EntityContainer Name="C"><EntitySet Name="InformationRegister_Rates" EntityType="A.Record"/>
        <EntitySet Name="AccumulationRegister_Stock" EntityType="A.Record"/>{functions}</EntityContainer></Schema>'''
        functions = []
        for name, periods in [("SliceLast", ["Period"]), ("SliceFirst", ["Period"]), ("Balance", ["Period"]),
                              ("Turnovers", ["StartPeriod", "EndPeriod"]), ("BalanceAndTurnovers", ["StartPeriod", "EndPeriod"])]:
            functions.append('<FunctionImport Name="' + name + '" ReturnType="Collection(A.Result)" IsBindable="true" '
                             'IsSideEffecting="false" m:HttpMethod="GET"><Parameter Name="bindingParameter" Type="Collection(A.Record)"/>'
                             + ''.join('<Parameter Name="' + p + '" Type="Edm.DateTime"/>' for p in periods)
                             + '<Parameter Name="Condition" Type="Edm.String"/></FunctionImport>')
        self.raw = schema.format(functions=''.join(functions)).encode()
        self.client = EntityClient(self.service, parse_metadata(self.raw))

    def test_all_register_tables_and_escaped_condition(self):
        for table, entity, method, kwargs in [
                ("slice-last", self.info, "SliceLast", {"period": "2026-10-23"}),
                ("slice-first", self.info, "SliceFirst", {}),
                ("balance", self.accum, "Balance", {"period": "2026-10-23"}),
                ("turnovers", self.accum, "Turnovers", {"start_period": "2026-10-01", "end_period": "2026-11-01"}),
                ("balance-and-turnovers", self.accum, "BalanceAndTurnovers", {})]:
            with self.subTest(table=table), patch.object(self.service, "request", return_value={"value": [{"Amount": "12.34"}]}) as request:
                result = self.client.register(entity, table, condition=comparison("eq", "Warehouse", "O'Brien / North"),
                                              select=["Amount"], **kwargs)
            self.assertEqual(result, [{"Amount": "12.34"}])
            method_arg, address = request.call_args.args
            self.assertEqual(method_arg, "GET")
            decoded = url.unquote(address)
            self.assertIn(entity + '/' + method + '(', decoded)
            self.assertIn("Condition='(Warehouse eq ''O''''Brien / North'')'", decoded)
            self.assertNotIn("$filter", url.parse_qs(url.urlsplit(address).query))
            self.assertNotIn(" / North", address)

    def test_condition_guid_and_declared_result_fields(self):
        with patch.object(self.service, "request", return_value={"value": []}) as request:
            self.client.register(self.info, "slice-last", condition=comparison("eq", "Currency", IDS["item"]))
        self.assertIn("guid''" + IDS["item"] + "''", url.unquote(request.call_args.args[1]))
        with self.assertRaises(Error):
            self.client.register(self.info, "slice-last", select=["Currency"])
        with self.assertRaises(Error):
            self.client.register(self.info, "slice-last", condition=comparison("eq", "Missing", "x"))

    def test_wrong_register_period_and_unsafe_function_fail_before_request(self):
        for entity, table, kwargs in [(self.info, "balance", {}), (self.accum, "slice-last", {}),
                                      (self.accum, "balance", {"start_period": "2026-10-01"}),
                                      (self.accum, "turnovers", {"period": "2026-10-01"}),
                                      (self.info, "slice-last", {"period": "bad"})]:
            with self.subTest(table=table), patch.object(self.service, "request") as request, self.assertRaises(Error):
                self.client.register(entity, table, **kwargs)
            request.assert_not_called()
        variants = [self.raw.replace(b'IsSideEffecting="false"', b'IsSideEffecting="true"'),
                    self.raw.replace(b'm:HttpMethod="GET"', b'm:HttpMethod="POST"'),
                    self.raw.replace(b'ReturnType="Collection(A.Result)"', b'ReturnType="Collection(A.Missing)"'),
                    self.raw.replace(b'IsBindable="true"', b'IsBindable="false"'),
                    self.raw.replace(b'Name="SliceLast"', b'Name="Post"')]
        for raw in variants:
            with self.subTest(raw=raw), patch.object(self.service, "request") as request, self.assertRaises(Error) as caught:
                EntityClient(self.service, parse_metadata(raw)).register(self.info, "slice-last")
            self.assertEqual(caught.exception.code, "operation_not_supported")
            request.assert_not_called()

    def test_register_cli_is_read_only(self):
        env = {"ONEC_ODATA_URL": self.service.base, "ONEC_ODATA_USERNAME": "u", "ONEC_ODATA_PASSWORD": "p"}
        with patch.dict(os.environ, env, clear=True), patch("onec_lib.cli.Transport", return_value=self.service) as factory, \
                patch.object(self.service, "request", side_effect=[self.raw, {"value": []}]), \
                patch("builtins.open", side_effect=AssertionError("Unexpected file access")):
            result = run(parser().parse_args(["register", self.info, "slice-last", "--period", "2026-10-23"]))
        self.assertEqual(result, {"ok": True, "items": []})
        self.assertFalse(factory.call_args.kwargs["allow_mutations"])

    def test_incomplete_ambiguous_and_required_function_parameters(self):
        first_function = b'<FunctionImport' + self.raw.split(b'<FunctionImport', 1)[1].split(b'</FunctionImport>', 1)[0] + b'</FunctionImport>'
        duplicate = self.raw.replace(b'</EntityContainer>', first_function + b'</EntityContainer>')
        variants = [self.raw.replace(b'Type="Edm.DateTime"', b'NoType="date"'),
                    self.raw.replace(b'<Parameter Name="Period" Type="Edm.DateTime"/>', b''), duplicate]
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(Error) as caught:
                EntityClient(self.service, parse_metadata(raw)).register(self.info, "slice-last", period="2026-10-23")
            self.assertEqual(caught.exception.code, "operation_not_supported")
        raw = self.raw.replace(b'Name="Period" Type="Edm.DateTime"', b'Name="Period" Type="Edm.DateTime" Nullable="false"')
        with self.assertRaises(Error) as caught:
            EntityClient(self.service, parse_metadata(raw)).register(self.info, "slice-last")
        self.assertEqual(caught.exception.code, "invalid_input")
