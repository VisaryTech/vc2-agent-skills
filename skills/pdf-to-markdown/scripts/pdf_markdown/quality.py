"""Versioned, explainable heuristics; none proves extraction completeness."""
from collections import Counter
import re
import unicodedata

import fitz

PROFILE = {
    "version": 1, "bad_ratio": .02, "repeat_chars": 12,
    "repeat_line_count": 4, "repeat_line_ratio": .5,
    "min_density_chars": 40, "min_chars_per_point2": .0002,
    "image_area_ratio": .005, "confidence": .85,
    "overlap": .5, "max_regions": 8, "region_attempts": 2,
    "retry_dpi": 450, "max_retry_dpi": 600,
    "ocr_max_side": 4000,
}


def flag(code, bbox=None, **details):
    return {"code": code, "bbox": list(bbox) if bbox is not None else None,
            "details": details}


def overlap(a, b):
    a, b = fitz.Rect(a), fitz.Rect(b)
    return (a & b).get_area() / max(1e-9, min(a.get_area(), b.get_area()))


def text_metrics(text, area=0):
    chars = [c for c in text if not c.isspace()]
    bad = sum(c == '\ufffd' or unicodedata.category(c) in ('Cc', 'Co', 'Cs', 'Cn')
              for c in chars)
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    counts = Counter(lines)
    repeated = sum(n for n in counts.values() if n >= PROFILE['repeat_line_count'])
    return {"characters": len(chars), "bad_ratio": bad / max(1, len(chars)),
            "letters": sum(c.isalpha() for c in chars),
            "digits": sum(c.isdigit() for c in chars),
            "symbols": sum(unicodedata.category(c)[0] in 'PS' for c in chars),
            "repeated_line_ratio": repeated / max(1, len(lines)),
            "long_repetition": bool(re.search(r'(.)\1{' + str(PROFILE['repeat_chars'] - 1) + r',}', text)),
            "chars_per_point2": len(chars) / max(1, area)}


def text_flags(text, bbox=None):
    metrics = text_metrics(text)
    result = []
    if metrics['bad_ratio'] >= PROFILE['bad_ratio']:
        result.append(flag('damaged_characters', bbox, ratio=metrics['bad_ratio']))
    if metrics['long_repetition'] or metrics['repeated_line_ratio'] >= PROFILE['repeat_line_ratio']:
        result.append(flag('repetitive_text', bbox))
    return result


def assess_native(blocks, images, page_area):
    text = '\n'.join(b['text'] for b in blocks)
    metrics = text_metrics(text, page_area)
    result = []
    if not metrics['characters']:
        result.append(flag('no_native_text'))
    for block in blocks:
        result.extend(text_flags(block['text'], block['bbox']))
    if metrics['repeated_line_ratio'] >= PROFILE['repeat_line_ratio']:
        result.append(flag('repetitive_page'))
    # A title or a short formula alone is not a density failure.
    if (metrics['characters'] >= PROFILE['min_density_chars'] and
            metrics['chars_per_point2'] < PROFILE['min_chars_per_point2']):
        result.append(flag('sparse_text', characters=metrics['characters']))
    lines = [line for block in blocks for line in block['lines']]
    for i, line in enumerate(lines):
        if any(line['text'].strip() == other['text'].strip() and
               overlap(line['bbox'], other['bbox']) > PROFILE['overlap']
               for other in lines[:i]):
            result.append(flag('overlapping_native_duplicates', line['bbox']))
    for box in images:
        if fitz.Rect(box).get_area() >= page_area * PROFILE['image_area_ratio']:
            result.append(flag('image_text_unconfirmed', box))
    return {"profile_version": PROFILE['version'], "metrics": metrics, "flags": result}


def assess_ocr(lines, blocks, images):
    result = []
    if not lines:
        result.append(flag('empty_ocr'))
    native = [line for b in blocks for line in b['lines']]
    for line in lines:
        if line['confidence'] < PROFILE['confidence']:
            result.append(flag('low_confidence', line['bbox'], confidence=line['confidence']))
        result.extend(text_flags(line['text'], line['bbox']))
        for other in native:
            if (overlap(line['bbox'], other['bbox']) > PROFILE['overlap'] and
                    line['text'].strip() != other['text'].strip()):
                result.append(flag('source_disagreement', line['bbox'],
                                   native=other['text'], ocr=line['text']))
    for box in images:
        if not any(overlap(box, line['bbox']) > PROFILE['overlap'] for line in lines):
            result.append(flag('uncovered_image', box))
    return result
