# Документы и черновики

Документация проверена 19.10.2026. Реализация проверена автономными тестами с подменённым транспортом; реальные чтения, скачивания и записи через API не выполнялись.

Примеры запускаются из каталога скилла. Замени `BOX_ID`, `MESSAGE_ID`, `ENTITY_ID`, `RECIPIENT_BOX_ID` и UUID операции реальными значениями. JSON, токены и выгрузки размещай вне репозитория. Глобальные аргументы указываются до команды; `--input -` и `--filter -` читают JSON из stdin. Локальные `FilePath` разрешаются относительно текущего каталога запуска.

## Поиск, чтение и статус

Сохрани `/tmp/diadoc-filter.json`:

```json
{
  "DocumentCategory": "Incoming",
  "FromDocumentDate": "01.10.2026",
  "ToDocumentDate": "19.10.2026",
  "Count": 100
}
```

```bash
python3 scripts/diadoc.py --box-id BOX_ID list --filter /tmp/diadoc-filter.json
python3 scripts/diadoc.py --box-id BOX_ID list --filter /tmp/diadoc-filter.json --all --max-pages 500
python3 scripts/diadoc.py --box-id BOX_ID read MESSAGE_ID
python3 scripts/diadoc.py --box-id BOX_ID read MESSAGE_ID --entity-id ENTITY_ID
```

Поиск использует [GetDocuments V4](https://developer.kontur.ru/doc/diadoc-api/http/GetDocuments_V4.html). Вход — объект [GetDocumentsV4Request](https://developer.kontur.ru/doc/diadoc-api/proto/GetDocumentsV4Request.html) без обёртки. Обязательная `DocumentCategory`: `Any`, `Incoming`, `Outgoing`, `Internal`, `Proxy` или `Deleted`. Клиент разрешает `Count` от 1 до 100, по умолчанию 100. Остальные фильтры и их сочетания проверяет сервер. Не смешивай фильтры дат документа и меток времени.

Без `--all` возвращается одна страница. С `--all` клиент передаёт `IndexKey` последнего документа в `AfterIndexKey` следующего запроса до неполной либо пустой страницы. Повторяющийся или отсутствующий курсор вызывает `pagination_stalled`. Лимит `--max-pages` по умолчанию 10000, считается от начального курсора. При достижении лимита на полной странице возвращается `pagination_limit`, без частичного результата; для подтверждения конца может понадобиться дополнительный запрос пустой страницы. `TotalCount` и `HasMoreResults` остаются исходными полями последнего ответа, а не вычисленным итогом; собранные документы находятся в `Documents`, добавляется `all_pages: true`. Постраничное чтение не является снимком изменяющихся данных.

`read` получает сообщение через [GetMessage V6](https://developer.kontur.ru/doc/diadoc-api/http/GetMessage_V6.html), без включения бинарного содержимого. При `--entity-id` запрос ограничивается связанными сущностями и дополнительно вызывает [GetDocument V3](https://developer.kontur.ru/doc/diadoc-api/http/GetDocument.html); результат содержит `Message` и `Document`. Без флага возвращается исходный объект сообщения. Сохраняются статусы API, включая сведения о документообороте, подписях, аннулировании и доставке. Один признак наличия подписи не означает завершение документооборота.

Обычный `list` предназначен для документов. Для чтения черновика нужен известный MessageId; поиск черновиков по ленте событий в эту версию не входит. Черновик и исходящий неотправленный документ с отложенной отправкой — разные сущности.

## Скачивание

```bash
python3 scripts/diadoc.py --box-id BOX_ID download MESSAGE_ID --output /tmp/diadoc-export
python3 scripts/diadoc.py --box-id BOX_ID download MESSAGE_ID --entity-id ENTITY_ID --output /tmp/diadoc-export
```

Клиент заново читает сообщение, затем скачивает сущности `Attachment` и `Signature` через [GetEntityContent V4](https://developer.kontur.ru/doc/diadoc-api/http/GetEntityContent.html). При `--entity-id` выгружаются связанные сущности, возвращённые GetMessage для выбранного документа. Подписи возвращаются в стандартном представлении API; отдельного режима оригинальных подписей нет. Архивы не распаковываются, печатные формы не генерируются.

Разделители путей и управляющие символы в имени заменяются, ведущие/замыкающие точки и пробелы убираются. Имена ограничены 180 байтами UTF-8 без разрыва символов; при совпадении добавляется числовой префикс. Существующие файлы и символьные ссылки не перезаписываются. Новый каталог создаётся с правами `0700`, файлы — `0600` на POSIX. Лимит одного ответа/файла — 100 МиБ. При ошибке уже скачанные файлы остаются в каталоге; атомарная выгрузка всего сообщения не гарантируется.

## Создание черновика

Сохрани `/tmp/diadoc-draft.json`:

```json
{
  "FromBoxId": "BOX_ID",
  "ToBoxId": "RECIPIENT_BOX_ID",
  "DocumentAttachments": [
    {
      "TypeNamedId": "Nonformalized",
      "FilePath": "/tmp/document.pdf",
      "Metadata": [{"Key": "FileName", "Value": "document.pdf"}],
      "NeedRecipientSignature": true
    }
  ]
}
```

```bash
python3 scripts/diadoc.py --box-id BOX_ID draft-create --input /tmp/diadoc-draft.json --operation-id 66666666-6666-4666-8666-666666666666 --dry-run
python3 scripts/diadoc.py --box-id BOX_ID draft-create --input /tmp/diadoc-draft.json --operation-id 66666666-6666-4666-8666-666666666666
```

Для каждой новой операции сгенерируй собственный UUID; пример нельзя постоянно переиспользовать. Сохрани его для восстановления результата. Предпросмотр не расходует ID операции на сервере.

Вход основан на [MessageToPost](https://developer.kontur.ru/doc/diadoc-api/proto/MessageToPost.html), вложения — на [DocumentAttachment](https://developer.kontur.ru/doc/diadoc-api/proto/DocumentAttachment.html). Вместо локального `FilePath` допускается `SignedContent: {"Content":"BASE64"}`; одновременно их указывать нельзя. `FilePath` — расширение CLI и в API не отправляется. Поддерживается от 1 до 40 вложений, суммарный JSON-запрос ограничен 100 МиБ. `Metadata.FileName` задавай самостоятельно; имя локального файла автоматически не добавляется. Формализованные XML должны быть уже подготовлены в нужном формате. Тип, функция, версия и метаданные должны соответствовать документу; их обязательность определяет API.

На уровне сообщения поддерживаются `FromBoxId`, `ToBoxId`, подразделения `FromDepartmentId`/`ToDepartmentId`, `IsInternal`, `ProxyBoxId`/`ProxyDepartmentId`, `IsDraft`, `LockDraft`, `StrictDraftValidation`, `DelaySend`, `LockPacket`, `LockMode`, `DocumentAttachments`. `FromBoxId` обязан соответствовать выбранному ящику. Получателя можно оставить незаполненным, если это допускает API для черновика. Для внутреннего сообщения `ToBoxId` не задаётся; подразделение получателя задаётся через `ToDepartmentId`.

Для вложений поддерживаются `SignedContent.Content`, `FilePath`, `Comment`, `NeedRecipientSignature`, `InitialDocumentIds`, `SubordinateDocumentIds`, `CustomDocumentId`, `NeedReceipt`, `CustomData`, `TypeNamedId`, `Function`, `Version`, `Metadata`, `WorkflowId`, `IsEncrypted`, `EditingSettingId`, `ParticipantRoles`. Шифрование не поддерживается: `IsEncrypted` может быть только `false`. Подписи, МЧД в SignedContent, полка документов и устаревшие массивы вложений не принимаются. Неизвестные верхнеуровневые поля и поля вложения вызывают отказ; бизнес-валидацию вложенных метаданных выполняет API.

Клиент устанавливает `IsDraft=true`; явное `false`, `DelaySend=true` и любые подписи отклоняются. Запись использует [PostMessage V3](https://developer.kontur.ru/doc/diadoc-api/http/PostMessage.html) с operationId. Это создание черновика без отправки контрагенту. Результат содержит `operation_id` и фактический объект `Message` с MessageId. Проверяй предупреждения и состояние результата: успешная загрузка не подтверждает готовность документа к подписанию.

## Изменённая копия черновика

Команды обновления существующего черновика нет. `draft-copy` создаёт новый черновик; оригинал остаётся без изменений. Входной patch — объект с необязательными полями `Message`, `Add`, `Replace`, `Remove`:

```json
{
  "Message": {"ToBoxId": "RECIPIENT_BOX_ID"},
  "Add": [
    {
      "TypeNamedId": "Nonformalized",
      "FilePath": "/tmp/extra.pdf",
      "Metadata": [{"Key": "FileName", "Value": "extra.pdf"}]
    }
  ],
  "Replace": {
    "ENTITY_ID": {
      "FilePath": "/tmp/revised.pdf",
      "Metadata": [{"Key": "FileName", "Value": "revised.pdf"}]
    }
  },
  "Remove": []
}
```

`Message` меняет поддерживаемые поля сообщения, кроме массива вложений. `Add` добавляет полные описания DocumentAttachment. `Replace` объединяет поля описания с восстановленным вложением по EntityId; при FilePath или SignedContent заменяется содержимое. Массивы, включая Metadata, заменяются целиком. `Remove` исключает вложения по EntityId. Неизвестный ID, дублирование удаления или сочетание замены и удаления одного ID отклоняются. Пустой patch `{}` создаёт копию без изменений.

```bash
python3 scripts/diadoc.py --box-id BOX_ID draft-copy MESSAGE_ID --input /tmp/diadoc-patch.json --operation-id 88888888-8888-4888-8888-888888888888 --dry-run
python3 scripts/diadoc.py --box-id BOX_ID draft-copy MESSAGE_ID --input /tmp/diadoc-patch.json --operation-id 88888888-8888-4888-8888-888888888888
```

Источник должен быть действующим черновиком своего ящика: используется актуальный `MessageType=Draft`, при отсутствии MessageType — старый `IsDraft=true`. Удалённые, утилизированные, преобразованные и заблокированные источники отклоняются. Копирование тестовых черновиков и цепочек через промежуточного получателя не поддерживается.

Сохраняются содержимое оставшихся файлов, метаданные, тип/функция/версия, WorkflowId, пользовательские данные, внешние идентификаторы и связи с другими сообщениями, флаги запроса подписи/извещения, настройка редактирования, подразделения и UTF-8 комментарии. Из FileName сущности восстанавливается метаданный FileName, если его нет в Metadata. Подписи, шифрование, удалённые вложения, нестандартные роли участников, внутренние связи пакета, разные подразделения вложений и неизвестные служебные сущности приводят к `unsupported_copy`. Это действует и для исключаемых вложений: команда не должна незаметно терять непонятные данные. При замене вложения новый файл и его метаданные задавай явно.

Копирование читает источник один раз и создаёт новый объект по полученному состоянию; оно не блокирует параллельные изменения оригинала. Перед запуском проверь dry-run и источник. Ответ содержит `source_message_id`, `new_message_id`, `operation_id`, `Message`. Старый черновик автоматически не удаляется.

## Незавершённая операция и повтор

Автоматических повторов нет. По контракту PostMessage повтор с тем же operationId возвращает результат первой успешной операции. UUID относится к одной логической операции, а не к документу вообще.

- `operation_pending`: операция ещё выполняется. Если возвращён `retry_after_seconds`, выжди этот интервал перед повтором. Если интервал не указан, проверь результат в Диадоке перед повтором. Нельзя считать HTTP 204 успешным созданием документа.
- `write_outcome_unknown`: таймаут, сетевая ошибка, HTTP 5xx либо некорректный ответ записи. Сохрани operation_id; сначала проверь черновики в интерфейсе Диадока. При повторе используй тот же ID и неизменный подготовленный запрос.
- Не изменяй входной JSON, локальные файлы и источник `draft-copy` между попытками. Повтор копирования заново читает источник; если состояние изменилось, сначала восстанови результат исходной операции, не создавая новую вслепую.
- Новый UUID создаёт другую операцию и может привести к дубликату. Не генерируй новый ID только из-за ошибки ответа.

`--dry-run` создания работает без токена и без сети. Dry-run копирования требует чтения API, но не вызывает PostMessage. Он показывает `request` с числом символов Base64 вместо содержимого; серверная валидация при этом не выполняется.
