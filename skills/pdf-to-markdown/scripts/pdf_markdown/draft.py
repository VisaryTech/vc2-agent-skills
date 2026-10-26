"""Build an unverified Markdown draft from page evidence."""
import fitz

from .document import usable


def covered_by_native(line, blocks):
    rect = fitz.Rect(line["bbox"])
    if not rect.get_area():
        return False
    for block in blocks:
        for native in block["lines"]:
            # Geometry alone would discard OCR correcting a broken text layer.
            if (usable(native["text"]) and native["text"].strip() == line["text"].strip()
                    and (rect & fitz.Rect(native["bbox"])).get_area() / rect.get_area() > .5):
                return True
    return False


def page_draft(record):
    chunks = []
    if record["ocr_mode"] != "always" or record["status"] == "partial":
        chunks.extend((b["bbox"], b["text"]) for b in record["native_blocks"])
    for line in record["ocr_lines"]:
        if not line["text"].strip():
            continue
        if record["ocr_mode"] == "auto" and covered_by_native(line, record["native_blocks"]):
            continue
        text = line["text"]
        if line["confidence"] < .85:
            text = f"[требует сверки: {text}]"
        chunks.append((line["bbox"], text))
    # Geometry is only a candidate order: columns and mixed-layer duplicates
    # must be resolved by visual review, without deleting the raw evidence.
    chunks.sort(key=lambda item: (item[0][1], item[0][0]))
    return "\n\n".join(text for _, text in chunks if text.strip()) or "[неразборчиво]"


def build_draft(manifest, records):
    """Assemble a document draft, including failed and partial pages."""
    successful = manifest["processed_pages"]
    sections = ["# Автоматическое извлечение PDF", f"Исходный PDF: {manifest['source_name']}",
                f"Обработанные страницы: {', '.join(map(str, successful)) or 'нет'}.",
                "Это технический черновик: структура, полнота, таблицы и формулы не проверены."]
    if not manifest["complete"]:
        sections.append("Частичный результат: не все выбранные страницы обработаны.")
    if manifest["partial_pages"]:
        sections.append("Частично извлечённые страницы: "
                        + ", ".join(map(str, manifest["partial_pages"])) + ".")
    by_page = {r["pdf_page"]: r for r in records}
    for number in manifest["selected_pages"]:
        sections.append(f"## Страница {number}")
        sections.append(page_draft(by_page[number]) if number in by_page
                        else "[неразборчиво]\n\nСтраница не обработана из-за ошибки; см. замечания.")
    sections.append("## Замечания\n\nВизуальная сверка не выполнялась; порядок колонок и кандидаты таблиц не подтверждены.")
    for entry in manifest["pages"]:
        for issue in entry.get("issues", []) + ([entry["error"]] if entry["status"] == "error" else []):
            sections.append(f"- Страница {entry['pdf_page']}: {issue}")

    return "\n\n".join(sections) + "\n"
