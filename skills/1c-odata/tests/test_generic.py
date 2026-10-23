"""Behavioral coverage for arbitrary entities, typed stateless CRUD."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse as url
import uuid

from support import IDS, Response
from onec_lib.cli import main, parser, run
from onec_lib.common import Error, WireNumber, load_json, wire_json
from onec_lib.generic import EntityClient, Mutations
from onec_lib.metadata import parse_metadata
from onec_lib.transport import Config, Transport

RAW = '''<Schema Namespace="Demo" Alias="D" xmlns:m="urn:metadata">
<EntityType Name="Object"><Key><PropertyRef Name="Область"/><PropertyRef Name="Номер"/></Key>
<Property Name="Область" Type="Edm.String" Nullable="false" MaxLength="40"/>
<Property Name="Номер" Type="Edm.Int64" Nullable="false"/>
<Property Name="Название" Type="Edm.String" MaxLength="20"/>
<Property Name="Дата" Type="Edm.DateTime"/>
<Property Name="Момент" Type="Edm.DateTimeOffset"/>
<Property Name="Сумма" Type="Edm.Decimal" Precision="10" Scale="2"/>
<Property Name="Измерение" Type="Edm.Double"/>
<Property Name="Строки" Type="Collection(D.Row)"/>
<Property Name="Теги" Type="Collection(Edm.String)"/>
<Property Name="Состояние" Type="D.Status"/>
<Property Name="Ссылка" Type="Edm.Guid"/>
<Property Name="DeletionMark" Type="Edm.Boolean"/>
<NavigationProperty Name="Owner" Relationship="D.Owner" FromRole="Object" ToRole="Owner"/>
</EntityType>
<EntityType Name="Document"><Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Posted" Type="Edm.Boolean"/>
<Property Name="DeletionMark" Type="Edm.Boolean"/>
<Property Name="Number" Type="Edm.String"/>
</EntityType>
<ComplexType Name="Row"><Property Name="Значение" Type="Edm.Decimal" Scale="3"/></ComplexType>
<EnumType Name="Status"><Member Name="New"/><Member Name="Done"/></EnumType>
<EntityContainer Name="Container">
<EntitySet Name="ПроизвольнаяСущность" EntityType="D.Object"/>
<EntitySet Name="Document_Тест" EntityType="D.Document"/>
<EntitySet Name="Запрет" EntityType="D.Object" m:creatable="false" m:updatable="false" m:deletable="false"/>
<EntitySet Name="AnnotationDenied" EntityType="D.Object">
<Annotation Term="Org.OData.Capabilities.V1.UpdateRestrictions"><Record>
<PropertyValue Property="Updatable" Bool="false"/></Record></Annotation></EntitySet>
</EntityContainer></Schema>'''.encode()
ENTITY = 'ПроизвольнаяСущность'
KEY = {"Область": "Мск / O'Brien,=()?#%", "Номер": "9223372036854775807"}


class MemoryService:
    base = 'https://example.invalid/base/odata/'

    def __init__(self):
        self.metadata = parse_metadata(RAW)
        self.client = EntityClient(self, self.metadata)
        self.rows = {}
        self.calls = []
        self.fail_write = None
        self.fail_get = False
        self.hide_write = False
        self.omit_key = False
        self.response_shape = 'value'
        self.response_headers = {}
        self.write_headers = []
        self.seed(ENTITY, {**KEY, 'Название': 'Исходное', 'DeletionMark': False})
        self.doc_key = {'Ref_Key': IDS['item']}
        self.seed('Document_Тест', {**self.doc_key, 'Posted': False, 'DeletionMark': False, 'Number': '1'})

    def seed(self, entity, row):
        key = {name: row[name] for name in self.client.schema.entity(entity)['keys']}
        self.rows[self.client.address(entity, key)] = copy.deepcopy(row)

    def safe_url(self, address):
        return Transport.safe_url(self, address)

    def request(self, method, address, payload=None, metadata=False, headers=None, with_headers=False):
        if with_headers:
            return MemoryService.request(self, method, address, payload, metadata), dict(self.response_headers)
        if headers:
            self.write_headers.append(dict(headers))
        self.calls.append((method, address, copy.deepcopy(payload)))
        if metadata:
            return RAW
        path = address.removeprefix(self.base).split('?', 1)[0]
        entity = url.unquote(path.split('(', 1)[0])
        if method == 'GET':
            if self.fail_get:
                raise Error('temporary_error', 'Unavailable')
            if '(' in path:
                if path not in self.rows:
                    raise Error('http_error', 'Not found', http_status=404)
                return {'d': copy.deepcopy(self.rows[path])}
            page = [copy.deepcopy(row) for name, row in self.rows.items() if name.startswith(path + '(')]
            return {'d': {'results': page}} if self.response_shape == 'd' else {'value': page}
        if self.fail_write == 'rejected':
            raise Error('http_error', 'Denied', http_status=403)
        if not self.hide_write:
            if method == 'POST':
                row = copy.deepcopy(payload)
                spec = self.client.schema.entity(entity)
                for key in spec['keys']:
                    if key not in row:
                        row[key] = str(uuid.uuid4()) if spec['properties'][key]['Type'] == 'Edm.Guid' else 'generated'
                self.seed(entity, row)
            elif method == 'PATCH':
                self.rows[path].update(copy.deepcopy(payload))
                row = self.rows[path]
            elif method == 'DELETE':
                del self.rows[path]
                row = None
        else:
            row = None
        if self.fail_write == 'unknown':
            raise Error('write_outcome_unknown', 'Reply lost')
        return {} if self.omit_key else copy.deepcopy(row)


class GenericTests(unittest.TestCase):
    def setUp(self):
        self.service = MemoryService()
        self.client = self.service.client
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mutations = Mutations(self.client)

    def writes(self):
        return [call for call in self.service.calls if call[0] != 'GET']

    def execute(self, operation='update', data=None, key=KEY, entity=ENTITY, dry_run=False):
        return self.mutations.execute(entity, operation, data, key, dry_run)

    def test_discover_arbitrary_entity_and_nested_schema(self):
        self.assertEqual(self.client.entities('произвольная'), [{'name': ENTITY, 'type': 'Demo.Object'}])
        described = self.client.describe(ENTITY)
        self.assertEqual(described['keys'], ['Область', 'Номер'])
        self.assertEqual(described['types']['Demo.Status']['members'], ['New', 'Done'])
        self.assertIn('Owner', described['navigation'])
        self.assertIn('Demo.Row', described['types'])

    def test_composite_key_escaping_and_response_verification(self):
        self.assertEqual(self.client.get(ENTITY, KEY)['Название'], 'Исходное')
        address = self.service.calls[-1][1]
        self.assertIn("O''Brien", url.unquote(address))
        self.assertIn('%2F', address)
        self.assertIn('9223372036854775807L', address)
        for key in [{}, {'Область': 'x'}, {**KEY, 'extra': 1}, {**KEY, 'Номер': None}]:
            with self.subTest(key=key), self.assertRaises(Error):
                self.client.get(ENTITY, key)
        with patch.object(self.service, 'request', return_value={**KEY, 'Номер': '2'}):
            with self.assertRaises(Error):
                self.client.get(ENTITY, KEY)

    def test_string_numeric_and_guid_single_keys(self):
        for kind, value in [('Edm.String', "a'b"), ('Edm.Int32', 42), ('Edm.Guid', IDS['item'])]:
            metadata = copy.deepcopy(self.service.metadata)
            metadata['types']['Demo.Object']['keys'] = ['Область']
            metadata['types']['Demo.Object']['properties']['Область'] = {'Type': kind}
            client = EntityClient(self.service, metadata)
            with patch.object(self.service, 'request', return_value={'Область': value}):
                self.assertEqual(client.get(ENTITY, {'Область': value}), {'Область': value})

    def test_query_projection_ordering_and_exact_filter(self):
        result = self.client.query_entity(ENTITY, KEY, ['Название'], ['Номер desc'])
        self.assertEqual(result, [{'Название': 'Исходное'}])
        params = url.parse_qs(url.urlsplit(self.service.calls[-1][1]).query)
        self.assertEqual(params['$orderby'], ['Номер desc'])
        self.assertIn('Область', params['$select'][0])
        with self.assertRaises(Error):
            self.client.query_entity(ENTITY, {'Название': 'Другое'})
        with self.assertRaises(Error):
            self.client.query_entity(ENTITY, order_by=['Номер desc,evil'])
        self.service.response_shape = 'd'
        self.assertEqual(len(self.client.query_entity(ENTITY, limit=None)), 1)
        self.assertNotIn('%24top', self.service.calls[-1][1])

    def test_query_pagination_boundaries_and_unsafe_links(self):
        for link in ['https://evil.invalid/x', self.service.base + ENTITY + '?loop=1']:
            response = {'value': [], 'odata.nextLink': link}
            with patch.object(self.service, 'request', return_value=response), self.assertRaises(Error):
                self.client.query_entity(ENTITY)
        with patch.object(self.service, 'request', side_effect=[{'d': {'results': [], '__next': '?page=2'}}, {'value': [KEY]}]):
            self.assertEqual(self.client.query_entity(ENTITY), [KEY])

    def test_create_update_mark_and_delete(self):
        new_key = {'Область': 'Новая', 'Номер': '2'}
        created = self.execute('create', {**new_key, 'Название': 'Создано'}, key=None)
        self.assertEqual(created['key'], new_key)
        self.execute(data={'Название': 'Обновлено'}, key=new_key)
        self.execute('mark-deletion', {'DeletionMark': True}, key=new_key)
        self.assertTrue(self.client.get(ENTITY, new_key)['DeletionMark'])
        self.execute('delete', key=new_key)
        self.assertIsNone(self.client.get(ENTITY, new_key, missing_ok=True))
        self.assertEqual([call[0] for call in self.writes()], ['POST', 'PATCH', 'PATCH', 'DELETE'])

    def test_dry_run_all_operations_do_not_write(self):
        for operation, data, key in [('create', {**KEY}, None), ('update', {'Название': 'x'}, KEY),
                                     ('mark-deletion', {'DeletionMark': True}, KEY), ('delete', None, KEY)]:
            result = self.execute(operation, data, key=key, dry_run=True)
            self.assertTrue(result['dry_run'])
        self.assertEqual(self.writes(), [])

    def test_each_explicit_call_is_a_new_write(self):
        first = self.execute('create', {'Number': '2'}, None, 'Document_Тест')
        second = self.execute('create', {'Number': '2'}, None, 'Document_Тест')
        self.assertNotEqual(first['key'], second['key'])
        self.assertEqual(len(self.writes()), 2)

    def test_unknown_write_reports_known_key_without_retry(self):
        self.service.fail_write = 'unknown'
        for operation, data in [('update', {'Название': 'x'}), ('delete', None)]:
            self.service.seed(ENTITY, {**KEY, 'Название': 'Исходное'})
            before = len(self.writes())
            with self.subTest(operation=operation), self.assertRaises(Error) as caught:
                self.execute(operation, data)
            self.assertEqual(caught.exception.code, 'write_outcome_unknown')
            self.assertEqual(caught.exception.details['key'], KEY)
            self.assertEqual(caught.exception.details['entity_set'], ENTITY)
            self.assertEqual(len(self.writes()), before + 1)

    def test_unknown_create_reports_explicit_key_or_null(self):
        self.service.fail_write = 'unknown'
        for data, key, entity in [({'Number': '2'}, None, 'Document_Тест'), (KEY, KEY, ENTITY)]:
            with self.subTest(entity=entity), self.assertRaises(Error) as caught:
                self.execute('create', data, None, entity)
            self.assertEqual(caught.exception.code, 'write_outcome_unknown')
            self.assertEqual(caught.exception.details['key'], key)
        self.assertEqual(len(self.writes()), 2)

    def test_read_failure_after_create_returns_server_key(self):
        original = self.service.request
        def request(method, *args, **kwargs):
            result = original(method, *args, **kwargs)
            if method == 'POST':
                self.service.fail_get = True
            return result
        with patch.object(self.service, 'request', side_effect=request), self.assertRaises(Error) as caught:
            self.execute('create', {'Number': '2'}, None, 'Document_Тест')
        self.assertEqual(caught.exception.code, 'write_outcome_unknown')
        self.assertIn('Ref_Key', caught.exception.details['key'])
        self.assertEqual(caught.exception.details['verification_error'], 'temporary_error')
        self.assertEqual(len(self.writes()), 1)

    def test_create_without_returned_key_and_explicit_key_fallback(self):
        self.service.omit_key = True
        with self.assertRaises(Error) as caught:
            self.execute('create', {'Number': '2'}, None, 'Document_Тест')
        self.assertEqual(caught.exception.code, 'write_outcome_unknown')
        self.assertIsNone(caught.exception.details['key'])
        self.assertEqual(self.execute('create', KEY, None)['key'], KEY)
        self.assertEqual(len(self.writes()), 2)

    def test_verification_mismatch_and_rejection_do_not_retry(self):
        self.service.hide_write = True
        with self.assertRaises(Error) as caught:
            self.execute(data={'Название': 'x'})
        self.assertEqual(caught.exception.code, 'write_outcome_unknown')
        self.assertEqual(caught.exception.details['verification_error'], 'verification_failed')
        self.service.fail_write = 'rejected'
        with self.assertRaises(Error) as caught:
            self.execute(data={'Название': 'x'})
        self.assertEqual(caught.exception.code, 'http_error')
        self.assertEqual(len(self.writes()), 2)

    def test_cli_metadata_reads_and_writes_do_not_access_filesystem(self):
        env = {'ONEC_ODATA_URL': self.service.base, 'ONEC_ODATA_USERNAME': 'integration',
               'ONEC_ODATA_PASSWORD': 'secret'}
        commands = [['check'], ['discover'], ['entities'], ['describe', ENTITY],
                    ['query', ENTITY], ['get', ENTITY, '--key', json.dumps(KEY)],
                    ['mark-deletion', ENTITY, '--key', json.dumps(KEY), '--dry-run'],
                    ['mark-deletion', ENTITY, '--key', json.dumps(KEY)]]
        with patch.dict(os.environ, env, clear=True), patch('onec_lib.cli.Transport', return_value=self.service), \
             patch('builtins.open', side_effect=AssertionError('Unexpected file access')), \
             patch('os.open', side_effect=AssertionError('Unexpected file access')), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('Unexpected directory creation')):
            for command in commands:
                with self.subTest(command=command):
                    result = run(parser().parse_args(command))
                    self.assertTrue(result['ok'])
                    if command == ['discover']:
                        self.assertEqual(result['metadata'], self.service.metadata)
                        self.assertNotIn('metadata_path', result)

    def test_metadata_denials_and_posted_documents_prevent_writes(self):
        for operation, data, key in [('create', KEY, None), ('update', {'Название': 'x'}, KEY), ('delete', None, KEY)]:
            self.service.seed('Запрет', {**KEY})
            with self.subTest(operation=operation), self.assertRaises(Error):
                self.execute(operation, data, key=key, entity='Запрет')
        self.service.seed('AnnotationDenied', {**KEY})
        with self.assertRaises(Error):
            self.execute(data={'Название': 'x'}, entity='AnnotationDenied')
        for operation in ['update', 'mark-deletion', 'delete']:
            self.service.seed('Document_Тест', {**self.service.doc_key, 'Posted': True})
            data = {'DeletionMark': True} if operation == 'mark-deletion' else {'Number': '2'} if operation == 'update' else None
            with self.subTest(operation=operation), self.assertRaises(Error):
                self.execute(operation, data, self.service.doc_key, 'Document_Тест')
        with self.assertRaises(Error):
            self.execute('create', {'Posted': True}, None, 'Document_Тест')
        self.assertEqual(self.writes(), [])

    def test_document_create_forces_unposted(self):
        self.execute('create', {'Number': '2'}, None, 'Document_Тест')
        self.assertFalse(self.writes()[0][2]['Posted'])

    def test_validation_and_table_replacement(self):
        data = {'Дата': '2026-10-23T16:17:18.123456', 'Момент': '2026-10-23T16:17:18+03:00',
                'Сумма': '123.45', 'Измерение': '1234567890123.45', 'Строки': [{'Значение': '1.234'}],
                'Теги': ['x', 'y'], 'Состояние': 'Done', 'Ссылка': '00000000-0000-0000-0000-000000000000'}
        self.execute(data=data)
        row = self.client.get(ENTITY, KEY)
        self.assertEqual(row['Дата'], data['Дата'])
        self.assertEqual(row['Момент'], data['Момент'])
        self.assertIsInstance(self.writes()[0][2]['Измерение'], WireNumber)
        self.execute(data={'Строки': []})
        self.assertEqual(self.client.get(ENTITY, KEY)['Строки'], [])
        invalid = [{'unknown': 'x'}, {'Название': 'x'*21}, {'Область': None}, {'Состояние': 'Wrong'},
                   {'Момент': '2026-10-23T12:00:00'}, {'Дата': '2026-10-23T12:00:00Z'},
                   {'Сумма': '0.001'}, {'Сумма': '123456789.00'}, {'Измерение': 'NaN'},
                   {'Номер': '9223372036854775808'}, {'Строки': [{'Значение': '1.2345'}]}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(Error):
                self.execute(data=data, dry_run=True)

    def test_exact_json_numbers_nullable_and_integer_ranges(self):
        path = Path(self.temp.name) / 'payload.json'
        path.write_text('{"Измерение":123456789012345.12345}', encoding='utf-8')
        data = self.client.encode(self.client.schema.entity(ENTITY), load_json(path, exact=True))
        self.assertIn('123456789012345.12345', wire_json(data))
        self.assertIsNone(self.client.value({'Type': 'Edm.String'}, None))
        for kind, value in [('Edm.Byte', -1), ('Edm.Int16', 32768), ('Edm.Int32', 2**31), ('Edm.Int64', True)]:
            with self.subTest(kind=kind), self.assertRaises(Error):
                self.client.value({'Type': kind}, value)
        self.assertTrue(self.client.equal({'Type': 'Edm.DateTimeOffset'}, '/Date(0)/', '1970-01-01T03:00:00+03:00'))
        self.assertFalse(self.client.equal({'Type': 'Edm.DateTime'}, '1970-01-01T00:00:01', '1970-01-01T00:00:00'))

    def test_cli_and_arbitrary_login(self):
        env = {'ONEC_ODATA_URL': self.service.base, 'ONEC_ODATA_USERNAME': 'integration',
               'ONEC_ODATA_PASSWORD': 'secret'}
        path = Path(self.temp.name) / 'payload.json'
        path.write_text('{"Название":"CLI"}', encoding='utf-8')
        with patch.dict(os.environ, env, clear=True), patch('onec_lib.cli.Transport', return_value=self.service):
            self.assertEqual(Config().username, 'integration')
            commands = [['entities', '--search', 'Произвольная'], ['describe', ENTITY],
                        ['get', ENTITY, '--key', json.dumps(KEY)], ['query', ENTITY, '--where', json.dumps(KEY)],
                        ['update', ENTITY, str(path), '--key', json.dumps(KEY), '--dry-run'],
                        ['update', ENTITY, str(path), '--key', json.dumps(KEY)]]
            for args in commands:
                stream = io.StringIO()
                with self.subTest(args=args), redirect_stdout(stream):
                    self.assertEqual(main(args), 0, stream.getvalue())
                    self.assertTrue(json.loads(stream.getvalue())['ok'])


    def test_cli_read_and_dry_run_disable_transport_mutations(self):
        env = {'ONEC_ODATA_URL': self.service.base, 'ONEC_ODATA_USERNAME': 'integration',
               'ONEC_ODATA_PASSWORD': 'secret'}
        with patch.dict(os.environ, env, clear=True), patch('onec_lib.cli.Transport', return_value=self.service) as factory:
            for command in [['check'], ['discover'], ['query', ENTITY],
                            ['delete', ENTITY, '--key', json.dumps(KEY), '--dry-run']]:
                run(parser().parse_args(command))
                self.assertFalse(factory.call_args.kwargs['allow_mutations'])
            run(parser().parse_args(['delete', ENTITY, '--key', json.dumps(KEY)]))
            self.assertTrue(factory.call_args.kwargs['allow_mutations'])


    def test_transport_patch_delete_do_not_retry_and_handle_empty_response(self):
        config = type('Config', (), {'base': self.service.base, 'username': 'u', 'password': 'p'})()
        transport = Transport(config, allow_mutations=True)
        for method in ['PATCH', 'DELETE']:
            with self.subTest(method=method), patch.object(transport.opener, 'open', side_effect=OSError()) as opened:
                with self.assertRaises(Error) as caught:
                    transport.request(method, 'Entity')
                self.assertEqual(caught.exception.code, 'write_outcome_unknown')
                self.assertEqual(opened.call_count, 1)
            with patch.object(transport.opener, 'open', return_value=Response(status=204, body=b'')):
                self.assertIsNone(transport.request(method, 'Entity'))

    def test_external_metadata_restrictions_and_inherited_navigation(self):
        raw = RAW.replace(b'</Schema>', b'''<Annotations Target="D.Container/AnnotationDenied">
<Annotation Term="Capabilities.DeleteRestrictions"><Record>
<PropertyValue Property="Deletable"><Bool>false</Bool></PropertyValue>
</Record></Annotation></Annotations>
<EntityType Name="Child" BaseType="D.Object"><NavigationProperty Name="Extra"/></EntityType>
</Schema>''')
        client = EntityClient(self.service, parse_metadata(raw))
        with self.assertRaises(Error) as caught:
            client.check_write('AnnotationDenied', 'delete', {})
        self.assertEqual(caught.exception.code, 'write_forbidden')
        self.assertEqual(set(client.schema.type('Demo.Child')['navigation']), {'Owner', 'Extra'})

    def test_missing_deletion_field_and_key_changes_fail_before_write(self):
        metadata = copy.deepcopy(self.service.metadata)
        del metadata['types']['Demo.Object']['properties']['DeletionMark']
        client = EntityClient(self.service, metadata)
        with self.assertRaises(Error):
            Mutations(client).execute(ENTITY, 'mark-deletion', {'DeletionMark': True}, KEY)
        with self.assertRaises(Error):
            self.execute(data={'Номер': '2'})
        self.assertEqual(self.writes(), [])

