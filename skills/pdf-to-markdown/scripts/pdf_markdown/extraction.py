"""Page extraction, immutable attempts, resumable selection and publication."""
import logging
from pathlib import Path
import shutil
import uuid

import fitz

from .cache import SCHEMA, cache_key, digest_file, load_cached_page, model_signature, save_json, settings_for
from .document import native_content, native_tables, open_pdf, pages_arg, vector_content
from .ocr import OCRConfig, PaddleEngine, automatic_models
from .quality import assess_native, flag
from .retries import run_ocr
from .review import load_records, publish, read_json
from .structure import build_elements

LOG = logging.getLogger('pdf-to-markdown')


def process_page(page, pix, key, ocr_mode, engine, directory, dpi):
    blocks, images = native_content(page)
    tables, table_error = native_tables(page)
    vectors, vector_regions = vector_content(page)
    quality = assess_native(blocks, images, page.rect.get_area())
    quality['flags'].extend(flag('vector_content_unverified', box) for box in vector_regions)
    run = ocr_mode == 'always' or (ocr_mode == 'auto' and quality['flags'])
    record = {'schema': SCHEMA, 'cache_key': key, 'pdf_page': page.number + 1,
              'status': 'success', 'execution_status': 'completed', 'review_status': 'pending',
              'ocr_mode': ocr_mode, 'size_points': list(page.rect * page.derotation_matrix),
              'rotation': page.rotation, 'coordinate_space': 'unrotated PDF points, top-left origin',
              'native_blocks': blocks, 'image_regions': images, 'table_candidates': tables,
              'vector_paths': vectors, 'vector_regions': vector_regions,
              'ocr_lines': [], 'attempts': [], 'issues': [], 'native_quality': quality,
              'quality_flags': list(quality['flags'])}
    if table_error:
        record['issues'].append(table_error)
        record['quality_flags'].append(flag('table_extraction_error', error=table_error))
    if run:
        lines, attempts, flags = run_ocr(page, pix, engine, directory, dpi, blocks,
                                       images + vector_regions, quality['flags'])
        record['ocr_lines'], record['attempts'] = lines, attempts
        record['quality_flags'].extend(flags)
        if any(a['error'] for a in attempts):
            record['status'], record['execution_status'] = 'partial', 'partial'
            for data in attempts:
                if data['error']:
                    record['issues'].append(f"Ошибка OCR: {data['error']}. Доступные данные сохранены.")
        if not lines:
            record['issues'].append('OCR не вернул текст: требуется визуальная сверка изображения.')
        defaults = automatic_models(engine.config.lang)
        record['resolved_models'] = {
            'det': model_signature(engine.config.det_model_dir) if engine.config.det_model_dir else
                defaults.get('PP-OCRv5_mobile_det'),
            'rec': model_signature(engine.config.rec_model_dir) if engine.config.rec_model_dir else
                defaults.get('cyrillic_PP-OCRv5_mobile_rec')}
        if any(value is None for value in record['resolved_models'].values()):
            record['quality_flags'].append(flag('model_fingerprint_unavailable'))
    elif ocr_mode == 'never' and quality['flags']:
        record['issues'].append('OCR отключён: подозрительные области требуют проверки.')
        record['quality_flags'].append(flag('ocr_disabled'))
    record['elements'] = build_elements(record)
    return record


def extract(args):
    source = Path(args.input).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if source == out / (source.stem + '.md'):
        raise ValueError('Нельзя перезаписать исходный файл')
    work = out / '.work'
    settings = settings_for(args)
    source_hash = digest_file(source)
    engine = PaddleEngine(OCRConfig(lang=args.lang, threads=args.threads,
                                   det_model_dir=args.det_model_dir, rec_model_dir=args.rec_model_dir))
    run_id = uuid.uuid4().hex
    with open_pdf(source) as doc:
        selected = pages_arg(args.pages, len(doc))
        previous = read_json(work / 'manifest.json', {})
        if previous:
            save_json(work / 'history' / f'{run_id}.json', previous)
        same = previous.get('source_sha256') == source_hash and previous.get('schema') == SCHEMA
        retained = [e for e in previous.get('pages', []) if e['pdf_page'] not in selected] if same else []
        manifest = {'schema': SCHEMA, 'run_id': run_id, 'source_name': source.name,
                    'source_sha256': source_hash, 'source_pages': len(doc), 'selected_pages': selected,
                    'document_pages': sorted(set(selected + [e['pdf_page'] for e in retained])),
                    'settings': settings, 'pages': retained, 'processed_pages': [], 'partial_pages': [],
                    'complete': False, 'execution_status': 'running', 'review_status': 'pending'}
        records = []
        for entry in retained:
            try:
                records.extend(load_records(work, {'pages': [entry]}))
            except (OSError, ValueError, TypeError):
                entry.update(status='error', execution_status='error', error='Повреждён сохранённый кэш; обновите страницу')
                entry.pop('cache', None)
        manifest['pages'].extend({'pdf_page': n, 'status': 'pending', 'execution_status': 'pending'} for n in selected)
        manifest['pages'].sort(key=lambda e: e['pdf_page'])
        # Publish pending pages immediately; an interruption cannot leave stale success.
        publish(out, manifest, records)
        for number in selected:
            key = cache_key(source_hash, number, settings)
            latest = work / 'cache' / f'{key}.json'
            record = load_cached_page(latest, key, number, args.ocr) if not args.refresh else None
            if record and not all(Path(a['input']).is_file() for a in record['attempts'] if a.get('input')):
                record = None
            entry = next(e for e in manifest['pages'] if e['pdf_page'] == number)
            try:
                page = doc[number - 1]
                if record is None:
                    directory = work / 'attempts' / key / run_id
                    cache = work / 'cache' / key / f'{run_id}.json'
                    preview = work / 'pages' / f'page-{number:04d}-{key[:16]}-{run_id}.png'
                    pix = page.get_pixmap(dpi=args.dpi, colorspace=fitz.csRGB, alpha=False)
                    preview.parent.mkdir(parents=True, exist_ok=True)
                    pix.save(preview)
                    record = process_page(page, pix, key, args.ocr, engine, directory, args.dpi)
                    record['source_sha256'] = source_hash
                    record['settings'] = settings
                    record['cache_path'] = cache.relative_to(work).as_posix()
                    record['preview'] = preview.relative_to(work).as_posix()
                    # Preserve a portable original for every suspicious page and formula/table.
                    if record['quality_flags']:
                        asset = out / 'assets' / preview.name
                        asset.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(preview, asset)
                        record['page_image'] = asset.relative_to(out).as_posix()
                    for element in record['elements']:
                        if element['type'] in ('table', 'formula', 'image'):
                            asset = out / 'assets' / f"p{number}-{element['id']}-{run_id}.png"
                            asset.parent.mkdir(parents=True, exist_ok=True)
                            clip = fitz.Rect(element['bbox']) * page.rotation_matrix & page.rect
                            if not clip.is_empty:
                                page.get_pixmap(dpi=args.dpi, clip=clip, alpha=False).save(asset)
                                element['image'] = asset.relative_to(out).as_posix()
                    save_json(cache, record)
                    save_json(latest, record)
                else:
                    preview = work / record['preview']
                    if not preview.exists():
                        preview.parent.mkdir(parents=True, exist_ok=True)
                        page.get_pixmap(dpi=args.dpi, colorspace=fitz.csRGB, alpha=False).save(preview)
                    asset = record.get('page_image')
                    if asset and not (out / asset).exists():
                        (out / asset).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(preview, out / asset)
                    for element in record['elements']:
                        if element.get('image') and not (out / element['image']).exists():
                            asset = out / element['image']
                            asset.parent.mkdir(parents=True, exist_ok=True)
                            clip = fitz.Rect(element['bbox']) * page.rotation_matrix & page.rect
                            page.get_pixmap(dpi=args.dpi, clip=clip, alpha=False).save(asset)
                records.append(record)
                entry.update(pdf_page=number, status=record['status'], execution_status=record['execution_status'],
                             cache=record['cache_path'], preview=record['preview'], issues=record['issues'],
                             cache_key=key)
            except Exception as exc:
                LOG.exception('Ошибка страницы %s', number)
                entry.update(status='error', execution_status='error', error=str(exc))
            manifest['processed_pages'] = [e['pdf_page'] for e in manifest['pages']
                if e['pdf_page'] in selected and e['status'] == 'success']
            manifest['partial_pages'] = [e['pdf_page'] for e in manifest['pages']
                if e['pdf_page'] in selected and e['status'] == 'partial']
            publish(out, manifest, records)
        manifest['complete'] = manifest['processed_pages'] == selected
        manifest['execution_status'] = 'completed' if manifest['complete'] else 'partial'
        manifest['available_pages'] = [e['pdf_page'] for e in manifest['pages'] if e['status'] == 'success']
        manifest['error_pages'] = [e['pdf_page'] for e in manifest['pages'] if e['status'] == 'error']
        publish(out, manifest, records)
        print(f"Markdown: {out / (source.stem + '.md')}\nМанифест: {work / 'manifest.json'}\n"
              f"Обработано {len(manifest['processed_pages'])}/{len(selected)} выбранных страниц. "
              f"Признаков качества: {len(manifest['quality_flags'])}; сверка: {manifest['review_status']}.")
        return 2 if not manifest['complete'] else 3 if args.fail_on_quality and manifest['quality_flags'] else 0
