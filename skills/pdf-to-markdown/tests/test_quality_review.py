"""Behavioral checks for quality, geometry, durable review and incremental extraction."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pdf_markdown import cli, document, extraction, quality, review, retries, structure
from pdf_markdown.ocr import PaddleEngine, OCRConfig


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.pdf'
        self.out = self.root / 'result'
        with fitz.open() as doc:
            for i in range(3):
                page = doc.new_page(width=300, height=300)
                page.insert_text((30, 40), f'Original text {i+1}')
            doc.save(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def extract(self, *extra):
        with redirect_stdout(io.StringIO()):
            return cli.main(['extract', str(self.source), '--out', str(self.out),
                             '--ocr', 'never', '--dpi', '72', *extra])

    def manifest(self):
        return review.read_json(self.out / '.work/manifest.json')

    def records(self):
        return review.load_records(self.out / '.work', self.manifest())

    def markdown(self):
        return (self.out / 'source.md').read_text(encoding='utf-8')

    def payload(self, scope='elements', result='recognition_error', corrected='Corrected text'):
        record = self.records()[0]
        elements = record['elements'] if scope == 'page' else record['elements'][:1]
        preview = self.manifest()['pages'][0]['preview']
        return {'source_sha256': self.manifest()['source_sha256'], 'reviews': [{
            'pdf_page': record['pdf_page'], 'scope': scope,
            'element_ids': [e['id'] for e in elements],
            'bbox': record['size_points'] if scope == 'page' else elements[0]['bbox'],
            'original': '\n\n'.join(e['text'] for e in elements), 'corrected': corrected,
            'result': result, 'basis': 'Compared glyphs against the page image',
            'evidence': ['.work/' + preview]}]}

    def register(self, payload):
        path = self.root / 'review.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            return cli.main(['review', str(path), '--out', str(self.out)])

    def test_incremental_refresh_keeps_other_pages_and_attempt_history(self):
        self.extract()
        before = self.manifest()
        self.extract('--pages', '2', '--refresh')
        after = self.manifest()
        self.assertEqual(after['selected_pages'], [2])
        self.assertEqual(after['document_pages'], [1, 2, 3])
        self.assertEqual(after['pages'][0]['cache'], before['pages'][0]['cache'])
        self.assertNotEqual(after['pages'][1]['cache'], before['pages'][1]['cache'])
        for entry in before['pages']:
            self.assertTrue((self.out / '.work' / entry['cache']).is_file())
        self.assertIn('Original text 3', self.markdown())

    def test_correction_survives_refresh_without_editing_automatic_evidence(self):
        self.extract('--pages', '1')
        payload = self.payload()
        self.assertEqual(self.register(payload), 0)
        self.assertIn('Corrected text', self.markdown())
        self.assertEqual(self.manifest()['review_status'], 'partial')
        self.extract('--pages', '1', '--refresh')
        self.assertIn('Corrected text', self.markdown())
        self.assertIn('Original text 1', self.records()[0]['elements'][0]['text'])
        self.assertEqual(self.manifest()['reviews'][0]['status'], 'verified')

    def test_changed_fragment_makes_correction_stale_but_keeps_it_in_journal(self):
        self.extract('--pages', '1')
        self.register(self.payload())
        native = extraction.native_content
        def changed(page):
            blocks, images = native(page)
            blocks[0]['text'] = 'Changed extraction'
            blocks[0]['lines'][0]['text'] = 'Changed extraction'
            return blocks, images
        with patch.object(extraction, 'native_content', side_effect=changed):
            self.extract('--pages', '1', '--refresh')
        self.assertEqual(self.manifest()['review_status'], 'stale')
        self.assertIn('Changed extraction', self.markdown())
        self.assertIn('Corrected text', self.markdown())
        journal = review.read_json(self.out / '.work/reviews.json')
        self.assertEqual(journal[0]['corrected'], 'Corrected text')

    def test_rejects_foreign_source_wrong_original_and_missing_evidence(self):
        self.extract('--pages', '1')
        payload = self.payload()
        for field, value in [('original', 'guessed text'), ('evidence', ['../outside.png']),
                             ('element_ids', ['unknown']), ('bbox', [0, 0, 1, 1])]:
            invalid = deepcopy(payload)
            invalid['reviews'][0][field] = value
            self.assertEqual(self.register(invalid), 2)
        payload['source_sha256'] = 'different source'
        self.assertEqual(self.register(payload), 2)
        self.assertFalse((self.out / '.work/reviews.json').exists())

    def test_review_allows_source_contradiction_without_rewriting_it(self):
        self.extract('--pages', '1')
        self.register(self.payload('page', 'source_contradiction', None))
        manifest = self.manifest()
        self.assertEqual(manifest['execution_status'], 'completed')
        self.assertEqual(manifest['review_status'], 'verified')
        self.assertIn('source_contradiction', {f['code'] for f in manifest['quality_flags']})
        self.assertIn('Original text 1', self.markdown())
        self.assertEqual(self.extract('--pages', '1', '--fail-on-quality'), 3)

    def test_unreadable_review_stays_visible(self):
        self.extract('--pages', '1')
        self.register(self.payload('page', 'unreadable', None))
        self.assertIn('[неразборчиво:', self.markdown())
        self.assertEqual(self.manifest()['review_status'], 'verified')
        self.assertTrue(self.manifest()['quality_flags'])

    def test_foreign_pdf_does_not_receive_old_corrections(self):
        self.extract('--pages', '1')
        self.register(self.payload())
        with fitz.open() as doc:
            doc.new_page().insert_text((30, 40), 'Another source')
            doc.save(self.root / 'other.pdf')
        self.source.write_bytes((self.root / 'other.pdf').read_bytes())
        self.extract('--pages', '1')
        self.assertNotIn('Corrected text', self.markdown())
        self.assertTrue(review.read_json(self.out / '.work/reviews.json'))

    def test_suspicious_text_triggers_ocr_and_raw_attempt_is_persisted(self):
        native = extraction.native_content
        def broken(page):
            blocks, images = native(page)
            blocks[0]['text'] = '\ufffd' * 20
            return blocks, images
        def predict(engine, pix, page):
            engine.last_raw = {'raw': 'original response'}
            return [{'text': 'Original text 1', 'confidence': .99, 'bbox': [30, 28, 100, 44]}]
        with patch.object(extraction, 'native_content', side_effect=broken), patch.object(PaddleEngine, 'predict', predict):
            self.assertEqual(self.extract('--pages', '1', '--ocr', 'auto', '--fail-on-quality'), 3)
        record = self.records()[0]
        self.assertTrue(record['attempts'])
        attempt = record['attempts'][0]
        self.assertEqual(attempt['raw_response'], {'raw': 'original response'})
        self.assertTrue(Path(attempt['input']).exists())
        self.assertEqual(record['execution_status'], 'completed')
        self.assertEqual(record['review_status'], 'pending')
        self.assertIn('damaged_characters', self.markdown())

    def test_interrupt_marks_pending_pages_and_resume_reuses_completed(self):
        original = extraction.native_content
        def interrupted(page):
            if page.number == 1:
                raise KeyboardInterrupt()
            return original(page)
        with patch.object(extraction, 'native_content', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.extract()
        self.assertFalse(self.manifest()['complete'])
        self.assertEqual(self.manifest()['pages'][1]['status'], 'pending')
        with patch.object(extraction, 'native_content', wraps=original) as native:
            self.extract()
            self.assertEqual(native.call_count, 2)

    def test_superseding_review_keeps_both_revisions(self):
        self.extract('--pages', '1')
        self.register(self.payload(corrected='First correction'))
        self.register(self.payload(corrected='Second correction'))
        self.assertEqual(len(review.read_json(self.out / '.work/reviews.json')), 2)
        self.assertEqual([e['status'] for e in self.manifest()['reviews']], ['superseded', 'verified'])
        self.assertIn('Second correction', self.markdown())
        self.assertNotIn('First correction', self.markdown())

    def test_images_are_portable_for_both_draft_and_final(self):
        with patch.object(PaddleEngine, 'predict', return_value=[]):
            self.extract('--pages', '1', '--ocr', 'always')
        for path in (self.out / 'source.md', self.out / '.work/draft.md'):
            links = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', path.read_text(encoding='utf-8'))
            self.assertTrue(links)
            self.assertTrue(all((path.parent / link).exists() for link in links))

    def test_full_page_review_supersedes_stale_element_review(self):
        self.extract('--pages', '1')
        self.register(self.payload())
        self.register(self.payload('page', 'confirmed', None))
        self.assertEqual(self.manifest()['review_status'], 'verified')
        self.assertEqual(self.manifest()['reviews'][0]['status'], 'superseded')
        self.assertNotIn('Corrected text', self.markdown())

    def test_invalid_review_batch_is_atomic(self):
        self.extract('--pages', '1')
        payload = self.payload()
        payload['reviews'].append({**payload['reviews'][0], 'element_ids': [['invalid']]})
        self.assertEqual(self.register(payload), 2)
        self.assertFalse((self.out / '.work/reviews.json').exists())

    def test_repetitive_pdf_auto_ocr_and_short_native_title_no_ocr(self):
        with patch.object(PaddleEngine, 'predict') as predict:
            self.extract('--pages', '1', '--ocr', 'auto')
            predict.assert_not_called()
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((20, 40), 'Z' * 40)
            doc.save(self.root / 'repeated.pdf')
        self.source.write_bytes((self.root / 'repeated.pdf').read_bytes())
        with patch.object(PaddleEngine, 'predict', return_value=[]) as predict:
            self.extract('--pages', '1', '--ocr', 'auto')
            self.assertGreater(predict.call_count, 1)
        self.assertIn('repetitive_text', {f['code'] for f in self.manifest()['quality_flags']})

    def test_missing_formula_asset_is_regenerated_on_cache_reuse(self):
        with fitz.open() as doc:
            doc.new_page().insert_text((30, 40), 'V = pi * r^2')
            doc.save(self.root / 'formula.pdf')
        self.source.write_bytes((self.root / 'formula.pdf').read_bytes())
        self.extract('--pages', '1')
        record = self.records()[0]
        image = self.out / record['elements'][0]['image']
        image.unlink()
        self.extract('--pages', '1')
        self.assertTrue(image.is_file())

    def test_vector_art_is_preserved_and_flagged_with_native_text(self):
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=300)
            page.insert_text((30, 40), 'Diagram')
            page.draw_rect(fitz.Rect(30, 80, 200, 200), color=(1, 0, 0))
            page.draw_line((30, 200), (200, 80))
            doc.save(self.root / 'vector.pdf')
        self.source.write_bytes((self.root / 'vector.pdf').read_bytes())
        self.extract('--pages', '1')
        record = self.records()[0]
        self.assertTrue(record['vector_paths'])
        self.assertIn('vector_content_unverified', {f['code'] for f in record['quality_flags']})
        graphics = [e for e in record['elements'] if e['provenance'][0]['source'] == 'vector_region']
        self.assertTrue(graphics)
        self.assertTrue((self.out / graphics[0]['image']).is_file())


class QualityAndGeometryTests(unittest.TestCase):
    def test_large_regions_are_tiled_below_detector_limit(self):
        boxes, omitted = retries.split_region([0, 0, 1500, 2000])
        self.assertLessEqual(len(boxes), quality.PROFILE['max_regions'] + 1)
        self.assertGreater(omitted, 0)
        for box in boxes:
            self.assertLessEqual(max(fitz.Rect(box).width, fitz.Rect(box).height) *
                                 quality.PROFILE['max_retry_dpi'] / 72, quality.PROFILE['ocr_max_side'])

    def test_short_text_numbers_and_formulas_are_not_bad_native_text(self):
        for text in ('Title', '0,0291 м³', 'V = pi * r^2', '12345'):
            block = {'text': text, 'bbox': [10, 10, 200, 30], 'lines': []}
            self.assertFalse(quality.assess_native([block], [], 100000)['flags'])
        self.assertTrue(quality.text_flags('\ufffd\ufffdabcdef'))
        self.assertTrue(quality.text_flags('x' * 40))
        self.assertTrue(quality.text_flags('\n'.join(['Repeated line'] * 5)))

    def test_columns_read_top_to_bottom_with_spanning_heading(self):
        def element(name, box, kind='paragraph'):
            return {'id': name, 'bbox': box, 'type': kind}
        elements = [element('R2', [170, 120, 250, 140]), element('L2', [20, 110, 100, 130]),
                    element('H', [20, 10, 260, 35], 'heading'),
                    element('R1', [170, 60, 250, 80]), element('L1', [20, 60, 100, 80])]
        self.assertEqual([e['id'] for e in structure.ordered(elements)], ['H', 'L1', 'L2', 'R1', 'R2'])

    def test_exact_duplicates_merge_provenance_disagreements_remain(self):
        box = [10, 10, 100, 30]
        line = {'text': '10,01', 'bbox': box, 'spans': [{'size': 11}]}
        record = {'pdf_page': 1, 'quality_flags': [], 'table_candidates': [],
                  'native_blocks': [{'text': '10,01', 'bbox': box, 'lines': [line]}],
                  'image_regions': [],
                  'ocr_lines': [{'text': '10,01', 'bbox': box, 'confidence': .99},
                                {'text': '10.01', 'bbox': box, 'confidence': .99}]}
        elements = structure.build_elements(record)
        self.assertEqual(len(elements), 1)
        self.assertEqual(len(elements[0]['provenance']), 2)
        self.assertEqual(elements[0]['alternatives'][0]['text'], '10.01')
        self.assertIn('source_disagreement', {f['code'] for f in record['quality_flags']})

    def test_retry_bounds_and_explicit_limit(self):
        with tempfile.TemporaryDirectory() as temp, fitz.open() as doc:
            page = doc.new_page(width=300, height=300)
            lines = [{'text': 'unclear', 'confidence': .1, 'bbox': [10, 10 + i*20, 60, 20 + i*20]}
                     for i in range(10)]
            engine = PaddleEngine(OCRConfig())
            with patch.object(engine, 'predict', return_value=deepcopy(lines)) as predict:
                _, attempts, flags = retries.run_ocr(page, page.get_pixmap(dpi=72), engine,
                                                     temp, 72, [], [], [])
            self.assertEqual(predict.call_count, 17)
            self.assertEqual(len(attempts), 17)
            self.assertIn('retry_region_limit', {f['code'] for f in flags})
            self.assertIn('retry_exhausted', {f['code'] for f in flags})

    def test_region_pixel_transform_accounts_for_clip_page_rotation_and_retry_rotation(self):
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=200)
            for rotation in (0, 90, 180, 270):
                page.set_rotation(rotation)
                for enhanced in (False, True):
                    pix, proxy, matrix = retries.region_input(page, [20, 30, 100, 70], 144, enhanced)
                    # Full OCR rectangle on a crop must recover the original crop extent.
                    fake = types.SimpleNamespace(json={'res': {'rec_texts': ['0,01'], 'rec_scores': [.9],
                         'rec_boxes': [[0, 0, pix.width, pix.height]]}})
                    engine = PaddleEngine(OCRConfig())
                    engine.engine = types.SimpleNamespace(predict=lambda _: [fake])
                    line = engine.predict(pix, proxy)[0]
                    for actual, expected in zip(line['bbox'], [20, 30, 100, 70]):
                        self.assertAlmostEqual(actual, expected, places=3)


if __name__ == '__main__':
    unittest.main()
