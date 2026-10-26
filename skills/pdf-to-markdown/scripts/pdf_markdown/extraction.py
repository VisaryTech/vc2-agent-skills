"""Coordinate page extraction, caching and progress publication."""
import logging
from pathlib import Path

import fitz

from .cache import (
    SCHEMA, atomic_text, cache_key, digest_file, load_cached_page, save_json, settings_for,
)
from .document import native_content, native_tables, needs_ocr, open_pdf, pages_arg
from .draft import build_draft
from .ocr import OCRConfig, PaddleEngine

LOG = logging.getLogger("pdf-to-markdown")


def process_page(page, pix, key, ocr_mode, engine):
    """Extract page evidence, retaining native data if OCR fails."""
    blocks, images = native_content(page)
    tables, table_error = native_tables(page)
    run_ocr = ocr_mode == "always" or (ocr_mode == "auto" and needs_ocr(blocks, images, page.rect.get_area()))
    record = {"schema": SCHEMA, "cache_key": key, "pdf_page": page.number + 1,
              "status": "success", "ocr_mode": ocr_mode,
              "size_points": list(page.rect), "rotation": page.rotation,
              "coordinate_space": "unrotated PDF points, top-left origin",
              "native_blocks": blocks, "image_regions": images,
              "table_candidates": tables, "ocr_lines": [], "issues": []}
    if table_error:
        record["issues"].append(table_error)
    if run_ocr:
        try:
            record["ocr_lines"] = engine.predict(pix, page)
        except Exception as exc:
            LOG.exception("Ошибка OCR страницы %s", page.number + 1)
            record["status"] = "partial"
            record["issues"].append(
                f"Ошибка OCR: {exc}. Сохранены текстовый слой и кандидаты таблиц; "
                "распознавание страницы не завершено")
        else:
            if not record["ocr_lines"]:
                record["issues"].append(
                    "OCR не вернул текст: сверьте изображение страницы, "
                    "включая области без текстового слоя")
    elif ocr_mode == "never" and needs_ocr(blocks, images, page.rect.get_area()):
        record["issues"].append("OCR отключён: области изображения или повреждённый текст требуют проверки")
    if any(l["confidence"] < .85 for l in record["ocr_lines"]):
        record["issues"].append("Есть OCR-фрагменты с confidence < 0.85")
    return record


def extract(args):
    source = Path(args.input).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    work = out / ".work"
    settings = settings_for(args)
    source_hash = digest_file(source)
    engine = PaddleEngine(OCRConfig(lang=args.lang, threads=args.threads,
                                   det_model_dir=args.det_model_dir,
                                   rec_model_dir=args.rec_model_dir))
    with open_pdf(source) as doc:
        selected = pages_arg(args.pages, len(doc))
        manifest = {"schema": SCHEMA, "source_name": source.name, "source_sha256": source_hash,
                    "source_pages": len(doc), "selected_pages": selected,
                    "settings": settings, "pages": [], "processed_pages": [], "partial_pages": [],
                    "complete": False, "review_status": "pending"}
        records = []
        # Write immediately: a killed run must not leave an old complete manifest.
        save_json(work / "manifest.json", manifest)
        for number in selected:
            key = cache_key(source_hash, number, settings)
            cache = work / "cache" / f"{key}.json"
            preview = work / "pages" / f"page-{number:04d}-{key[:16]}.png"
            record = load_cached_page(cache, key, number, args.ocr) if not args.refresh else None
            try:
                page = doc[number - 1]
                pix = None
                if record is None or not preview.exists():
                    pix = page.get_pixmap(dpi=args.dpi, colorspace=fitz.csRGB, alpha=False)
                    preview.parent.mkdir(parents=True, exist_ok=True)
                    pix.save(preview)
                if record is None:
                    record = process_page(page, pix, key, args.ocr, engine)
                    save_json(cache, record)
                records.append(record)
                progress = "processed_pages" if record["status"] == "success" else "partial_pages"
                manifest[progress].append(number)
                manifest["pages"].append({"pdf_page": number, "status": record["status"],
                                          "cache": str(cache.relative_to(work)),
                                          "preview": str(preview.relative_to(work)),
                                          "issues": record["issues"]})
            except Exception as exc:
                LOG.exception("Ошибка страницы %s", number)
                manifest["pages"].append({"pdf_page": number, "status": "error", "error": str(exc)})
            save_json(work / "manifest.json", manifest)
            LOG.info("Страница %s: %s", number, manifest["pages"][-1]["status"])
        successful = [r["pdf_page"] for r in records if r["status"] == "success"]
        complete = successful == selected
        manifest["processed_pages"] = successful
        manifest["complete"] = complete
        save_json(work / "manifest.json", manifest)
        atomic_text(work / "draft.md", build_draft(manifest, records))
        print(f"Черновик: {work / 'draft.md'}\nМанифест: {work / 'manifest.json'}\n"
              f"Обработано {len(successful)}/{len(selected)} страниц. Визуальная сверка не выполнена.")
        return 0 if complete else 2
