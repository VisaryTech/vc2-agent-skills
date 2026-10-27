"""Append-only review journal, independent of replaceable extraction caches."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import uuid

import fitz

from .cache import atomic_text, digest_file, save_json
from .quality import flag


def read_json(path, default=None):
    if not Path(path).exists():
        return default
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_records(work, manifest):
    records = []
    for entry in manifest['pages']:
        if entry.get('cache'):
            record = read_json(work / entry['cache'])
            if record:
                records.append(record)
    return records


def apply_reviews(manifest, records, journal):
    records = deepcopy(records)
    by_page = {r['pdf_page']: r for r in records}
    states = []
    latest = {}
    superseded = {key for item in journal for key in item.get('supersedes', [])}
    for item in journal:
        if item['source_sha256'] == manifest['source_sha256']:
            latest[(item['pdf_page'], item['scope'], tuple(item['element_ids']))] = item['id']
    for item in journal:
        if item['source_sha256'] != manifest['source_sha256']:
            continue
        state = {'id': item['id'], 'pdf_page': item['pdf_page'], 'status': 'superseded'}
        state.update(scope=item['scope'], bbox=item['bbox'], element_ids=item['element_ids'])
        states.append(state)
        if (item['id'] in superseded or
                latest[(item['pdf_page'], item['scope'], tuple(item['element_ids']))] != item['id']):
            continue
        record = by_page.get(item['pdf_page'])
        elements = {e['id']: e for e in record['elements']} if record else {}
        stale = (not record or any(elements.get(key, {}).get('fingerprint') != value
                                   for key, value in item['fingerprints'].items()))
        if item['scope'] == 'page' and set(elements) != set(item['element_ids']):
            stale = True
        state['status'] = 'stale' if stale else 'verified'
        if not record:
            continue
        record.setdefault('review_notes', []).append({**state, 'result': item['result'],
                                                      'basis': item['basis'], 'corrected': item['corrected']})
        if stale:
            record['quality_flags'].append(flag('stale_review', item['bbox'], review_id=item['id']))
            continue
        for key in item['element_ids']:
            elements[key]['review_status'] = 'verified'
            elements[key]['review_id'] = item['id']
            elements[key]['review_result'] = item['result']
        if item['corrected'] is not None and item['element_ids']:
            first, *rest = item['element_ids']
            elements[first]['rendered_text'] = item['corrected']
            for key in rest:
                elements[key]['rendered_text'] = ''
        # An element review resolves only flags within that exact region, not page-wide concerns.
        record['quality_flags'] = [f for f in record['quality_flags'] if
            f['code'] == 'model_fingerprint_unavailable' or not (
                item['scope'] == 'page' or (f.get('bbox') and any(
                    fitz.Rect(elements[key]['bbox']).contains(fitz.Rect(f['bbox']))
                    for key in item['element_ids'])))]
        if item['result'] in ('source_contradiction', 'unreadable'):
            record['quality_flags'].append(flag(item['result'], item['bbox'], review_id=item['id']))
        if item['scope'] == 'page':
            record['page_reviewed'] = True
    for record in records:
        relevant = [s for s in states if s['pdf_page'] == record['pdf_page']]
        checked = [e for e in record['elements'] if e.get('review_status') == 'verified']
        record['review_status'] = ('stale' if any(s['status'] == 'stale' for s in relevant) else
                                   'verified' if record.get('page_reviewed') else
                                   'partial' if checked else 'pending')
    return records, states


def publish(out, manifest, records):
    from .draft import build_draft
    work = out / '.work'
    journal = read_json(work / 'reviews.json', [])
    effective, states = apply_reviews(manifest, records, journal)
    manifest['reviews'] = states
    for entry in manifest['pages']:
        record = next((r for r in effective if r['pdf_page'] == entry['pdf_page']), None)
        if record:
            entry['quality_flags'] = record['quality_flags']
            entry['review_status'] = record['review_status']
    manifest['quality_flags'] = [{'pdf_page': r['pdf_page'], **f}
                                 for r in effective for f in r['quality_flags']]
    manifest['review_status'] = ('stale' if any(e.get('review_status') == 'stale' for e in manifest['pages']) else
        'verified' if manifest['pages'] and all(e.get('review_status') == 'verified' for e in manifest['pages']) else
        'partial' if any(e.get('review_status') in ('verified', 'partial') for e in manifest['pages']) else 'pending')
    save_json(work / 'manifest.json', manifest)
    automatic_manifest = {**manifest, 'review_status': 'pending'}
    atomic_text(work / 'draft.md', build_draft(automatic_manifest, records, '../'))
    atomic_text(out / (Path(manifest['source_name']).stem + '.md'), build_draft(manifest, effective))


def register(args):
    out = Path(args.out).expanduser().resolve()
    work = out / '.work'
    manifest = read_json(work / 'manifest.json')
    if not manifest:
        raise ValueError('Сначала выполните extract')
    payload = read_json(Path(args.input))
    if not isinstance(payload, dict) or payload.get('source_sha256') != manifest['source_sha256']:
        raise ValueError('Хеш PDF в проверке не совпадает с текущим документом')
    records = load_records(work, manifest)
    by_page = {r['pdf_page']: r for r in records}
    journal = read_json(work / 'reviews.json', [])
    additions = []
    if not isinstance(payload.get('reviews'), list) or not payload['reviews']:
        raise ValueError('Нужен непустой список reviews')
    for item in payload['reviews']:
        if not isinstance(item, dict):
            raise ValueError('Проверка должна быть объектом')
        record = by_page.get(item.get('pdf_page'))
        if not record:
            raise ValueError('Проверяемая страница отсутствует')
        elements = {e['id']: e for e in record['elements']}
        ids = item.get('element_ids')
        scope = item.get('scope', 'elements')
        if (scope not in ('elements', 'page') or not isinstance(ids, list) or
                not all(isinstance(key, str) for key in ids) or len(set(ids)) != len(ids)):
            raise ValueError('Некорректные scope / element_ids')
        if any(key not in elements for key in ids) or (not ids and scope != 'page'):
            raise ValueError('Неизвестные или отсутствующие элементы')
        if scope == 'page' and set(ids) != set(elements):
            raise ValueError('Полная сверка страницы должна охватывать все элементы')
        original = '\n\n'.join(elements[key]['text'] for key in ids)
        if item.get('original') != original:
            raise ValueError('original не совпадает с автоматическим извлечением')
        bbox = item.get('bbox')
        if (not isinstance(bbox, list) or len(bbox) != 4 or
                not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox)):
            raise ValueError('Нужен конечный bbox из четырёх чисел')
        rect = fitz.Rect(bbox)
        if rect.is_empty or not fitz.Rect(record['size_points']).contains(rect):
            raise ValueError('bbox вне страницы')
        if any(not rect.contains(fitz.Rect(elements[key]['bbox']) & fitz.Rect(record['size_points'])) for key in ids):
            raise ValueError('bbox должен охватывать проверенные элементы')
        if scope == 'page' and not rect.contains(fitz.Rect(record['size_points'])):
            raise ValueError('Полная сверка должна охватывать страницу')
        result = item.get('result')
        if result not in ('confirmed', 'recognition_error', 'source_contradiction', 'unreadable'):
            raise ValueError('Неизвестный result')
        corrected = item.get('corrected')
        if corrected is not None and not isinstance(corrected, str):
            raise ValueError('corrected должен быть строкой или null')
        if result == 'recognition_error' and corrected is None:
            raise ValueError('Для ошибки распознавания нужен corrected')
        if result == 'unreadable' and corrected is not None and '[неразборчиво' not in corrected:
            raise ValueError('Неразборчивое содержание должно иметь явную отметку [неразборчиво]')
        if result in ('confirmed', 'source_contradiction') and corrected not in (None, original):
            raise ValueError('Изменение текста требует result=recognition_error')
        if not isinstance(item.get('basis'), str) or not item['basis'].strip():
            raise ValueError('Нужно основание визуальной сверки')
        evidence = item.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('Нужны ссылки evidence на просмотренные изображения')
        for value in evidence:
            if not isinstance(value, str):
                raise ValueError('evidence должен содержать пути к PNG')
            path = (out / value).resolve()
            if not path.is_relative_to(out) or not path.is_file() or path.suffix.lower() != '.png':
                raise ValueError('evidence должен ссылаться на PNG внутри выходного каталога')
        supersedes = item.get('supersedes', [])
        previous = {old['id']: old for old in journal + additions if
                    old['source_sha256'] == manifest['source_sha256'] and old['pdf_page'] == item['pdf_page']}
        if (not isinstance(supersedes, list) or not all(isinstance(key, str) and key in previous
                                                       for key in supersedes)):
            raise ValueError('supersedes должен ссылаться на проверки той же страницы и PDF')
        supersedes = sorted(set(supersedes + [key for key, old in previous.items() if scope == 'page' or
            (old['scope'] == scope and set(old['element_ids']) == set(ids))]))
        additions.append({**item, 'scope': scope, 'source_sha256': manifest['source_sha256'],
                          'supersedes': supersedes,
                          'evidence_sha256': {value: digest_file(out / value) for value in evidence},
                          'corrected': corrected, 'id': uuid.uuid4().hex,
                          'created_at': datetime.now(timezone.utc).isoformat(),
                          'fingerprints': {key: elements[key]['fingerprint'] for key in ids}})
    save_json(work / 'reviews.json', journal + additions)
    publish(out, manifest, records)
    print(f'Зарегистрировано проверок: {len(additions)}. Итоговый Markdown обновлён.')
    return 0
