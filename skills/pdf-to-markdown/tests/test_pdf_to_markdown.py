import io
import json
from pathlib import Path
import shutil
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import fitz

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pdf_markdown import cache, cli, document, extraction
from pdf_markdown import ocr as ocr_adapter



class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        self.out = self.root / "result"
        with fitz.open() as doc:
            for number in range(1, 4):
                page = doc.new_page(width=500, height=650)
                page.insert_text((30, 40), f"Heading {number}", fontsize=18)
                page.insert_text((30, 85), "1. First paragraph\n2. Second paragraph")
                page.insert_text((30, 145), "0,000825 kg\n116.35 / 2.465 m3")
                page.insert_text((280, 85), "Right column\nV = pi * r^2")
            doc.save(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def run_extract(self, *extra):
        args = cli.parser().parse_args(["extract", str(self.source), "--out", str(self.out),
                                       "--ocr", "never", "--dpi", "72", *extra])
        with redirect_stdout(io.StringIO()):
            return extraction.extract(args)

    def manifest(self):
        return json.loads((self.out / ".work/manifest.json").read_text(encoding='utf-8'))

    def record(self, entry):
        return json.loads((self.out / ".work" / entry["cache"]).read_text(encoding='utf-8'))

    def test_cli_entry_point_from_another_directory(self):
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', str(SCRIPTS / "pdf_to_markdown.py"), "extract",
             str(self.source), "--out", str(self.out), "--pages", "2",
             "--ocr", "never", "--dpi", "72"],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest()["processed_pages"], [2])
        self.assertIn("Heading 2", (self.out / ".work/draft.md").read_text(encoding='utf-8'))

    def test_native_pages_structure_and_verbatim_values(self):
        self.assertEqual(self.run_extract("--pages", "3,1,3"), 0)
        manifest = self.manifest()
        self.assertEqual(manifest["processed_pages"], [1, 3])
        self.assertTrue(manifest["complete"])
        record = self.record(manifest["pages"][0])
        self.assertFalse(record["ocr_lines"])
        self.assertTrue(record["native_blocks"][0]["lines"][0]["spans"])
        draft = (self.out / ".work/draft.md").read_text(encoding='utf-8')
        self.assertIn("## Страница 1", draft)
        self.assertNotIn("## Страница 2", draft)
        self.assertIn("## Страница 3", draft)
        self.assertIn("0,000825 kg", draft)
        self.assertIn("116.35 / 2.465 m3", draft)
        self.assertIn("V = pi * r^2", draft)
        self.assertIn("Right column", draft)
        self.assertTrue((self.out / "source.md").exists())

    def test_resume_failed_page_and_reuse_success(self):
        original = extraction.native_content

        def fail_middle(page):
            if page.number == 1:
                raise RuntimeError("simulated failure")
            return original(page)

        with patch.object(extraction, "native_content", side_effect=fail_middle):
            self.assertEqual(self.run_extract(), 2)
        self.assertEqual(self.manifest()["processed_pages"], [1, 3])
        with patch.object(extraction, "native_content", wraps=original) as native:
            self.assertEqual(self.run_extract(), 0)
            self.assertEqual(native.call_count, 1)
        self.assertEqual(self.manifest()["processed_pages"], [1, 2, 3])

    def test_corrupt_cache_and_missing_preview_recovered(self):
        self.run_extract("--pages", "1")
        entry = self.manifest()["pages"][0]
        (self.out / ".work" / entry["cache"]).write_text("broken JSON")
        (self.out / ".work" / entry["preview"]).unlink()
        self.assertEqual(self.run_extract("--pages", "1"), 0)
        self.assertEqual(self.record(self.manifest()["pages"][0])["status"], "success")
        self.assertTrue((self.out / ".work" / self.manifest()['pages'][0]["preview"]).exists())

    def test_cache_invalidates_pdf_settings_models_and_refresh(self):
        self.run_extract("--pages", "1")
        first = self.manifest()["pages"][0]
        self.run_extract("--pages", "1", "--dpi", "80")
        self.assertNotEqual(first["cache"], self.manifest()["pages"][0]["cache"])
        self.run_extract("--pages", "1")
        replacement = self.root / "new.pdf"
        with fitz.open() as doc:
            doc.new_page().insert_text((20, 30), "Changed PDF")
            doc.save(replacement)
        shutil.move(replacement, self.source)
        self.run_extract("--pages", "1")
        self.assertNotEqual(first["preview"], self.manifest()["pages"][0]["preview"])
        self.assertNotEqual(first["cache"], self.manifest()["pages"][0]["cache"])
        with patch.object(extraction, "native_content", wraps=extraction.native_content) as native:
            self.run_extract("--pages", "1", "--refresh")
            self.assertEqual(native.call_count, 1)
        models = self.root / "model"
        models.mkdir()
        weights = models / "weights"
        weights.write_bytes(b"first")
        before = cache.model_signature(models)
        weights.write_bytes(b"other")
        self.assertNotEqual(before, cache.model_signature(models))

    def make_mixed_pdf(self):
        with fitz.open() as doc:
            page = doc.new_page(width=500, height=650)
            page.insert_text((30, 40), "Native heading")
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200), False)
            pix.clear_with(240)
            page.insert_image(fitz.Rect(30, 100, 300, 400), pixmap=pix)
            target = self.root / "mixed.pdf"
            doc.save(target)
        self.source = target

    def test_mixed_page_ocr_and_confidence_preserve_values(self):
        self.make_mixed_pdf()
        ocr = [{"text": "0,0291 м³", "confidence": .3, "bbox": [40, 130, 150, 150]}]
        with patch.object(ocr_adapter.PaddleEngine, "predict", return_value=ocr) as engine:
            self.assertEqual(self.run_extract("--ocr", "auto"), 0)
            self.assertEqual(engine.call_count, 3)
        draft = (self.out / ".work/draft.md").read_text(encoding='utf-8')
        self.assertIn("Native heading", draft)
        self.assertIn("[требует сверки: 0,0291 м³]", draft)

    def test_ocr_failure_preserves_partial_data_and_retries(self):
        self.make_mixed_pdf()
        tables = [{"status": "unverified", "rows": [["0,0291"]]}]
        for mode in ("auto", "always"):
            with self.subTest(mode=mode):
                self.out = self.root / mode
                with (
                    patch.object(ocr_adapter.PaddleEngine, "predict", side_effect=RuntimeError("OCR unavailable")),
                    patch.object(extraction, "native_tables", return_value=(tables, None)),
                ):
                    self.assertEqual(self.run_extract("--ocr", mode), 2)
                manifest = self.manifest()
                self.assertFalse(manifest["complete"])
                self.assertEqual(manifest["processed_pages"], [])
                self.assertEqual(manifest["partial_pages"], [1])
                entry = manifest["pages"][0]
                self.assertEqual(entry["status"], "partial")
                self.assertTrue((self.out / ".work" / entry["preview"]).is_file())
                record = self.record(entry)
                self.assertTrue(record["native_blocks"])
                self.assertEqual(record["table_candidates"], tables)
                self.assertIn("OCR unavailable", "\n".join(entry["issues"]))
                draft = (self.out / ".work/draft.md").read_text(encoding='utf-8')
                self.assertIn("Native heading", draft)
                self.assertIn("OCR unavailable", draft)
                ocr = [{"text": "Recovered text", "confidence": .99, "bbox": [40, 130, 150, 150]}]
                with patch.object(ocr_adapter.PaddleEngine, "predict", return_value=ocr) as engine:
                    self.assertEqual(self.run_extract("--ocr", mode), 0)
                    self.assertEqual(engine.call_count, 1)
                self.assertTrue(self.manifest()["complete"])
                self.assertEqual(self.manifest()["partial_pages"], [])
                self.assertEqual(self.record(self.manifest()["pages"][0])["status"], "success")
                with patch.object(ocr_adapter.PaddleEngine, "predict") as engine:
                    self.assertEqual(self.run_extract("--ocr", mode), 0)
                    engine.assert_not_called()

    def test_ocr_failure_continues_other_pages_and_reuses_them_on_retry(self):
        def fail_middle(pix, page):
            if page.number == 1:
                raise RuntimeError("OCR unavailable")
            return [{"text": f"OCR page {page.number + 1}", "confidence": .99,
                     "bbox": [30, 30, 150, 50]}]

        with patch.object(ocr_adapter.PaddleEngine, "predict", side_effect=fail_middle):
            self.assertEqual(self.run_extract("--ocr", "always"), 2)
        manifest = self.manifest()
        self.assertEqual(manifest["processed_pages"], [1, 3])
        self.assertEqual(manifest["partial_pages"], [2])
        draft = (self.out / ".work/draft.md").read_text(encoding='utf-8')
        self.assertIn("OCR page 1", draft)
        self.assertIn("Heading 2", draft)
        self.assertIn("OCR page 3", draft)
        recovered = [{"text": "Recovered page 2", "confidence": .99, "bbox": [30, 30, 150, 50]}]
        with patch.object(ocr_adapter.PaddleEngine, "predict", return_value=recovered) as engine:
            self.assertEqual(self.run_extract("--ocr", "always"), 0)
            self.assertEqual(engine.call_count, 3)
        self.assertEqual(self.manifest()["processed_pages"], [1, 2, 3])

    def test_empty_ocr_warns_even_with_native_text(self):
        self.make_mixed_pdf()
        for mode in ("auto", "always"):
            with self.subTest(mode=mode):
                self.out = self.root / mode
                with patch.object(ocr_adapter.PaddleEngine, "predict", return_value=[]) as engine:
                    self.assertEqual(self.run_extract("--ocr", mode), 0)
                    self.assertGreaterEqual(engine.call_count, 3)
                    self.assertLessEqual(engine.call_count, 17)
                entry = self.manifest()["pages"][0]
                self.assertTrue(any("OCR не вернул текст" in issue for issue in entry["issues"]))
                self.assertEqual(self.record(entry)["ocr_lines"], [])
                self.assertTrue(self.record(entry)["native_blocks"])
                draft = (self.out / ".work/draft.md").read_text(encoding='utf-8')
                self.assertIn(entry["issues"][0], draft)

    def test_table_candidates_keep_merged_cells(self):
        self.source = self.root / "table.pdf"
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=300)
            for y in [50, 100, 150]:
                page.draw_line((30, y), (270, y))
            for x in [30, 270]:
                page.draw_line((x, 50), (x, 150))
            page.draw_line((150, 100), (150, 150))
            page.insert_text((45, 80), "Merged header")
            page.insert_text((45, 130), "0,8283")
            page.insert_text((170, 130), "35.9934")
            doc.save(self.source)
        self.run_extract()
        tables = self.record(self.manifest()["pages"][0])["table_candidates"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["status"], "unverified")
        self.assertEqual(tables[0]["rows"][1], ["0,8283", "35.9934"])
        self.assertEqual(tables[0]["rows"][0], ["Merged header", None])
        self.assertIn([30.0, 50.0, 270.0, 100.0], tables[0]["cells"])

    def test_rotated_crop_and_portable_asset(self):
        rotated = self.root / "rotated.pdf"
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=200)
            page.draw_rect(fitz.Rect(20, 30, 100, 70), color=(1, 0, 0), fill=(1, 0, 0))
            page.set_rotation(90)
            doc.save(rotated)
        target = self.out / "assets/formula.png"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["crop", str(rotated), "--page", "1", "--bbox",
                                       "25,35,95,65", "--dpi", "72", "--out", str(target)]), 0)
        pix = fitz.Pixmap(target)
        self.assertEqual((pix.width, pix.height), (30, 70))
        self.assertEqual(tuple(pix.samples[:3]), (255, 0, 0))
        (self.out / "result.md").write_text("![Formula](assets/formula.png)")
        moved = self.root / "moved"
        shutil.copytree(self.out, moved)
        self.assertTrue((moved / "assets/formula.png").is_file())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["crop", str(rotated), "--page", "1", "--bbox",
                                       "0,0,999,999", "--out", str(target)]), 2)

    def test_invalid_page_ranges_and_bbox(self):
        for value in ["0", "4", "3-1", "1,,2", "", "-1", "1-99"]:
            with self.assertRaises(ValueError, msg=value):
                document.pages_arg(value, 3)
        for value in ["0,0,0,3", "nan,0,2,3", "0,0,2", "2,2,1,1"]:
            with self.assertRaises(Exception, msg=value):
                cli.bbox_arg(value)

    def test_ocr_pixel_boxes_convert_to_unrotated_points(self):
        # Exercise the actual adapter without loading model weights.
        import types
        fake = types.SimpleNamespace(json={"res": {"rec_texts": ["0,01"], "rec_scores": [.8],
                                                   "rec_boxes": [[20, 30, 60, 90]]}})
        adapter = ocr_adapter.PaddleEngine(ocr_adapter.OCRConfig())
        adapter.engine = types.SimpleNamespace(predict=lambda _: [fake])
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=200)
            page.set_rotation(90)
            pix = page.get_pixmap(dpi=72)
            # numpy is optional for the rest of the suite.
            try:
                import numpy
            except ImportError:
                self.skipTest("OCR adapter test needs numpy")
            line = adapter.predict(pix, page)[0]
            self.assertEqual(line["text"], "0,01")
            self.assertEqual(line["bbox"], [30.0, 140.0, 90.0, 180.0])


if __name__ == "__main__":
    unittest.main()
