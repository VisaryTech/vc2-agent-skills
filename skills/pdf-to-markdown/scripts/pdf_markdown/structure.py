"""Provisional layout with stable geometry identities and complete provenance."""
import hashlib
import json
import re
from statistics import median

import fitz

from .quality import PROFILE, flag, overlap


def identity(page, kind, bbox):
    data = [page, kind, [round(v, 2) for v in bbox]]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:20]


def fingerprint(element):
    # Attempt UUIDs and confidence changes alone must not invalidate a correction.
    data = {k: element.get(k) for k in ('id', 'bbox', 'text', 'type', 'rows', 'cells')}
    data['alternatives'] = [{'text': a['text'], 'bbox': a.get('bbox')} for a in element['alternatives']]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def ordered(elements):
    """Recursive vertical gutters, separated by spanning headings/elements."""
    if len(elements) < 2:
        return elements
    left = min(e['bbox'][0] for e in elements)
    right = max(e['bbox'][2] for e in elements)
    spanning = [e for e in elements if e['type'] == 'heading' or
                e['bbox'][2] - e['bbox'][0] > (right - left) * .7]
    if spanning and len(spanning) < len(elements):
        first = min(spanning, key=lambda e: e['bbox'][1])
        above = [e for e in elements if e is not first and e['bbox'][3] <= first['bbox'][1]]
        rest = [e for e in elements if e is not first and e not in above]
        if above or all(e['bbox'][1] >= first['bbox'][1] for e in rest):
            return ordered(above) + [first] + ordered(rest)
    intervals = sorted((e['bbox'][0], e['bbox'][2]) for e in elements)
    end = intervals[0][1]
    gaps = []
    for x0, x1 in intervals[1:]:
        if x0 - end > 10:
            gaps.append((x0 - end, (x0 + end) / 2))
        end = max(end, x1)
    if gaps:
        cut = max(gaps)[1]
        a = [e for e in elements if e['bbox'][2] <= cut]
        b = [e for e in elements if e['bbox'][0] >= cut]
        if a and b and len(a) + len(b) == len(elements):
            return ordered(a) + ordered(b)
    return sorted(elements, key=lambda e: (e['bbox'][1], e['bbox'][0], e['id']))


def build_elements(record):
    page = record['pdf_page']
    elements = []
    spans = [s for b in record['native_blocks'] for l in b['lines'] for s in l['spans']]
    normal_size = median([s['size'] for s in spans]) if spans else 11

    def add(kind, box, text, source, **extra):
        e = {"id": identity(page, kind, box), "pdf_page": page, "bbox": list(box),
             "type": kind, "text": text, "provenance": [source],
             "alternatives": [], "structure_status": "provisional", "review_status": "pending",
             "links": [], **extra}
        # Preserve distinct elements at identical geometry without duplicate IDs.
        if any(other['id'] == e['id'] for other in elements):
            e['id'] += '-' + str(sum(other['id'].startswith(e['id']) for other in elements))
        elements.append(e)
        return e

    for i, block in enumerate(record['native_blocks']):
        text = block['text']
        size = max((s['size'] for l in block['lines'] for s in l['spans']), default=normal_size)
        kind = 'paragraph'
        if size >= normal_size * 1.25:
            kind = 'heading'
        elif re.match(r'^\s*(?:[-•▪]|\d+[.)])\s+', text):
            kind = 'list'
        elif re.match(r'^\s*(?:Рис\.?|Таблица|Figure|Table)\s*\d', text, re.I):
            kind = 'caption'
        elif re.search(r'[=∑∫√]|\^', text):
            kind = 'formula'
        add(kind, block['bbox'], text, {'source': 'native', 'index': i})
    for i, line in enumerate(record['ocr_lines']):
        if not line['text'].strip():
            continue
        ref = {'source': 'ocr', 'index': i, 'attempt': line.get('attempt')}
        native_line_match = next((j for j, b in enumerate(record['native_blocks']) if any(
            native['text'].strip() == line['text'].strip() and
            overlap(native['bbox'], line['bbox']) > PROFILE['overlap'] for native in b['lines'])), None)
        if native_line_match is not None:
            owner = next(e for e in elements if e['provenance'][0] == {'source': 'native', 'index': native_line_match})
            owner['provenance'].append(ref)
            continue
        matches = [e for e in elements if e['type'] != 'table' and
                   overlap(e['bbox'], line['bbox']) > PROFILE['overlap']]
        duplicate = next((e for e in matches if e['text'].strip() == line['text'].strip()), None)
        if duplicate:
            duplicate['provenance'].append(ref)
            continue
        # Only combine whole comparable fragments. Partial overlaps remain visible.
        equivalent = next((e for e in matches if overlap(e['bbox'], line['bbox']) > .8 and
                           min(fitz.Rect(e['bbox']).get_area(), fitz.Rect(line['bbox']).get_area()) /
                           max(1, max(fitz.Rect(e['bbox']).get_area(), fitz.Rect(line['bbox']).get_area())) > .65), None)
        if equivalent:
            equivalent['alternatives'].append({'text': line['text'], 'provenance': ref,
                                                'confidence': line['confidence'], 'bbox': line['bbox']})
        else:
            kind = 'formula' if re.search(r'[=∑∫√]|\^', line['text']) else 'paragraph'
            add(kind, line['bbox'], line['text'], ref, confidence=line['confidence'])
    for i, table in enumerate(record['table_candidates']):
        if 'bbox' not in table:
            continue
        e = add('table', table['bbox'], '', {'source': 'table_candidate', 'index': i},
                rows=table['rows'], cells=table.get('cells', []))
        e['links'] = [{'relation': 'contains', 'target': other['id'], 'status': 'provisional'}
                      for other in elements if other is not e and
                      fitz.Rect(e['bbox']).contains(fitz.Rect(other['bbox']))]
    for i, box in enumerate(record['image_regions']):
        add('image', box, '', {'source': 'image_region', 'index': i})
    for i, box in enumerate(record.get('vector_regions', [])):
        add('image', box, '', {'source': 'vector_region', 'index': i})
    # Captions link to the closest table/formula/image, never asserted as verified.
    for e in elements:
        if e['type'] == 'caption':
            candidates = [o for o in elements if o['type'] in ('table', 'formula', 'image')]
            if candidates:
                near = min(candidates, key=lambda o: abs(o['bbox'][1] - e['bbox'][3]))
                e['links'].append({'relation': 'caption_of', 'target': near['id'], 'status': 'provisional'})
    elements = ordered(elements)
    for i, e in enumerate(elements):
        e['reading_order'] = i
        if i:
            e['links'].append({'relation': 'previous', 'target': elements[i-1]['id'], 'status': 'provisional'})
        if e['alternatives']:
            record['quality_flags'].append(flag('source_disagreement', e['bbox'], element_id=e['id']))
        if e['type'] in ('table', 'formula'):
            record['quality_flags'].append(flag('structure_unverified', e['bbox'], element_id=e['id']))
        e['fingerprint'] = fingerprint(e)
    # Overlapping layout outside table membership is ambiguous even at high OCR confidence.
    text = [e for e in elements if e['type'] not in ('table', 'image')]
    if any(overlap(e['bbox'], other['bbox']) > .2 for i, e in enumerate(text) for other in text[:i]):
        record['quality_flags'].append(flag('reading_order_ambiguous'))
    return elements
