"""Opt-in real-engine integration test; uses only explicitly supplied local weights."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pdf_markdown import cli, review


@unittest.skipUnless(os.environ.get('PDF_MARKDOWN_REAL_OCR') == '1', 'Opt in with PDF_MARKDOWN_REAL_OCR=1')
class RealOCRTest(unittest.TestCase):
    def test_scanned_text_values_and_raw_evidence(self):
        det = os.environ.get('PDF_MARKDOWN_DET_MODEL')
        rec = os.environ.get('PDF_MARKDOWN_REC_MODEL')
        self.assertTrue(det and rec, 'Set PDF_MARKDOWN_DET_MODEL and PDF_MARKDOWN_REC_MODEL')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with fitz.open() as native, fitz.open() as scan:
                page = native.new_page(width=450, height=230)
                page.insert_text((25, 45), 'PDF extraction sample', fontsize=20)
                page.insert_text((25, 90), 'Value: 0,0291 kg', fontsize=18)
                page.insert_text((25, 135), '1. First paragraph', fontsize=18)
                pix = page.get_pixmap(dpi=150, colorspace=fitz.csRGB, alpha=False)
                scan.new_page(width=450, height=230).insert_image(page.rect, pixmap=pix)
                scan.save(root / 'scan.pdf')
            out = root / 'out'
            with redirect_stdout(io.StringIO()):
                code = cli.main(['extract', str(root / 'scan.pdf'), '--out', str(out),
                    '--dpi', '150', '--threads', '2', '--det-model-dir', det, '--rec-model-dir', rec])
            self.assertEqual(code, 0)
            manifest = review.read_json(out / '.work/manifest.json')
            record = review.load_records(out / '.work', manifest)[0]
            text = '\n'.join(line['text'] for line in record['ocr_lines'])
            self.assertIn('PDF extraction sample', text)
            self.assertIn('0,0291', text)
            self.assertTrue(record['attempts'][0]['raw_response_available'])
            self.assertEqual(record['review_status'], 'pending')
            self.assertTrue(record['quality_flags'])
            self.assertTrue(all(record['resolved_models'].values()))


if __name__ == '__main__':
    unittest.main()
