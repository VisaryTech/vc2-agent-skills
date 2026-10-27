"""Markdown rendering of provisional layout and separately recorded visual review."""
import re

from .quality import PROFILE


def element_text(element):
    if 'rendered_text' in element:
        return element['rendered_text']
    text = element['text']
    checked = element.get('review_status') == 'verified'
    if element.get('review_result') == 'unreadable':
        return f'[неразборчиво: {text}]' if text else '[неразборчиво]'
    if not checked and (element.get('confidence', 1) < PROFILE['confidence'] or element['alternatives']):
        text = f'[требует сверки: {text}]'
    # Preserve source heading text, but reserve level 2 for physical page markers.
    text = re.sub(r'(?m)^(#{1,2})\s', r'\\\1 ', text)
    if element['type'] == 'heading':
        text = '### ' + text.replace('\n', '\n\n')
    if element['type'] == 'table':
        # Native cell assignments are evidence, not a confirmed Markdown table.
        rows = element.get('rows', [])
        label = 'Таблица: структура подтверждена визуально.' if checked else 'Таблица: структура ячеек требует сверки.'
        text = label + '\n\n' + '\n'.join(
            '    ' + ' | '.join('[не определено]' if cell is None else str(cell).replace('\n', '<br>')
                                 for cell in row) for row in rows)
    if element['type'] == 'image':
        text = 'Изображение из оригинала.'
    if not checked:
        for alternative in element['alternatives']:
            text += '\n\n[альтернативное распознавание: ' + alternative['text'] + ']'
    return text


def page_draft(record, asset_prefix=''):
    chunks = []
    for element in record['elements']:
        text = element_text(element)
        if not text:
            continue
        chunks.append(f"<!-- element:{element['id']} bbox:{element['bbox']} "
                      f"origin:{','.join(sorted({p['source'] for p in element['provenance']}))} "
                      f"review:{element.get('review_status', 'pending')} -->\n\n{text}")
        if element.get('image'):
            chunks.append(f"![{element['type']}, страница {record['pdf_page']}]({asset_prefix}{element['image']})")
    if record.get('page_image') and record['quality_flags']:
        chunks.append(f"![Оригинал страницы {record['pdf_page']} для сверки]({asset_prefix}{record['page_image']})")
    return '\n\n'.join(chunks) or '[неразборчиво: отсутствие текста не подтверждает пустую страницу]'


def build_draft(manifest, records, asset_prefix=''):
    sections = ['# Извлечение PDF', f"Исходный PDF: {manifest['source_name']}",
                f"SHA-256: `{manifest['source_sha256']}`",
                'Содержание извлечено автоматически; структура и полнота оцениваются эвристически.',
                f"Выполнение текущего запуска: {manifest['execution_status']}; "
                f"выбранные страницы: {', '.join(map(str, manifest['selected_pages']))}.",
                f"Визуальная сверка: {manifest.get('review_status', 'pending')}. "
                'Успешное выполнение и высокий confidence не подтверждают правильность.']
    if not manifest['complete']:
        sections.append('Частичный результат: не все выбранные страницы обработаны.')
    by_page = {r['pdf_page']: r for r in records}
    for number in manifest.get('document_pages', manifest['selected_pages']):
        sections.append(f'## Страница {number}')
        record = by_page.get(number)
        if not record:
            sections.append('[неразборчиво]\n\nСтраница не обработана; см. манифест и замечания.')
            continue
        sections.append(f"Выполнение: {record['execution_status']}; сверка: {record.get('review_status', 'pending')}.")
        sections.append(page_draft(record, asset_prefix))
        for issue in record['quality_flags']:
            sections.append(f"- Требует внимания: `{issue['code']}`; область: {issue.get('bbox')}; "
                            f"подробности: {issue.get('details', {})}.")
        for note in record.get('review_notes', []):
            if note['status'] == 'pending' and note['result'] == 'needs_review':
                sections.append(f"- [требует сверки: {note['basis']}] Область: {note['bbox']}; "
                                f"элементы: {note['element_ids']}; запись журнала: `{note['id']}`.")
                continue
            sections.append(f"- Визуальная проверка `{note['id']}`: {note['status']}, {note['result']}; "
                            f"охват: {note['scope']}, область: {note['bbox']}, элементы: {note['element_ids']}. {note['basis']}")
            if note['status'] == 'stale' and note['corrected'] is not None:
                sections.append('Сохранённое исправление требует повторной сверки:\n\n' + note['corrected'])
    sections.append('## Замечания')
    for entry in manifest['pages']:
        for issue in entry.get('issues', []) + ([entry['error']] if entry.get('error') else []):
            sections.append(f"- Страница {entry['pdf_page']}: {issue}")
    sections.append('Непроверенные элементы имеют статус pending. Проверка отдельных областей '
                    'не означает проверки всей страницы или документа.')
    return '\n\n'.join(sections) + '\n'
