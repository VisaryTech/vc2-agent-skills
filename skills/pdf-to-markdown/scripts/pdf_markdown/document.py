"""PDF access, native evidence, page selection and image rendering."""
import re
from pathlib import Path

import fitz


def pages_arg(value, count):
    if value is None:
        return list(range(1, count + 1))
    result = set()
    for part in value.split(","):
        if not re.fullmatch(r"\s*\d+\s*(?:-\s*\d+\s*)?", part):
            raise ValueError("Страницы: 1-3,7; нумерация начинается с 1")
        bounds = [int(v.strip()) for v in part.split("-")]
        first, last = bounds[0], bounds[-1]
        if first < 1 or last < first or last > count:
            raise ValueError(f"Диапазон страниц вне PDF (1–{count}): {part}")
        result.update(range(first, last + 1))
    return sorted(result)


def usable(text):
    chars = [c for c in text if not c.isspace()]
    return bool(chars) and sum(c == "\ufffd" or (ord(c) < 32) for c in chars) / len(chars) < .02


def native_content(page):
    # Avoid embedding image bytes in JSON. Coordinates use the unrotated PDF
    # coordinate space in points, matching get_text() and page.derotation_matrix.
    data = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES,
                         sort=True)
    blocks = []
    for block in data["blocks"]:
        if block["type"] != 0:
            continue
        lines = []
        for line in block["lines"]:
            spans = [{"text": s["text"], "bbox": list(s["bbox"]),
                      "size": s["size"], "font": s["font"], "flags": s["flags"]}
                     for s in line["spans"]]
            lines.append({"text": "".join(s["text"] for s in spans),
                          "bbox": list(line["bbox"]), "spans": spans})
        blocks.append({"bbox": list(block["bbox"]), "lines": lines,
                       "text": "\n".join(l["text"] for l in lines)})
    images = [list(i["bbox"]) for i in page.get_image_info()]
    return blocks, images


def needs_ocr(blocks, images, page_area):
    text = "\n".join(b["text"] for b in blocks)
    # Image regions may contain text even when a page also has native text.
    return not usable(text) or any(fitz.Rect(b).get_area() >= page_area * .005 for b in images)


def native_tables(page):
    tables = []
    try:
        for table in page.find_tables().tables:
            tables.append({"bbox": list(table.bbox), "rows": table.extract(),
                           "cells": [list(c) if c else None for c in table.cells],
                           "row_count": table.row_count, "col_count": table.col_count,
                           "status": "unverified"})
    except Exception as exc:
        return [], f"Не удалось извлечь кандидаты таблиц: {exc}"
    return tables, None


def open_pdf(source):
    doc = fitz.open(source)
    if not doc.is_pdf or doc.needs_pass:
        doc.close()
        raise ValueError("Нужен доступный PDF без пароля; зашифрованный PDF сначала разблокируйте")
    if not len(doc):
        doc.close()
        raise ValueError("В PDF нет страниц")
    return doc


def render(args):
    with open_pdf(args.input) as doc:
        if args.page < 1 or args.page > len(doc):
            raise ValueError(f"Страница вне PDF (1–{len(doc)})")
        page = doc[args.page - 1]
        clip = None
        if args.command == "crop":
            # User/cache coordinates are unrotated; get_pixmap clip is rotated.
            clip = args.bbox * page.rotation_matrix
            if not page.rect.contains(clip):
                raise ValueError("Область выходит за границы страницы")
        out = Path(args.out).expanduser()
        if out.suffix.lower() != ".png":
            raise ValueError("Изображение должно иметь расширение .png")
        if out.resolve() == Path(args.input).expanduser().resolve():
            raise ValueError("Нельзя перезаписать источник")
        out.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(dpi=args.dpi, clip=clip, colorspace=fitz.csRGB, alpha=False).save(out)
        print(out)
    return 0
