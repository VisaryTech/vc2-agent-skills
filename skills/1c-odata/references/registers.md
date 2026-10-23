# Виртуальные таблицы регистров

Используйте `register ENTITY TABLE`, когда нужны срез на дату, остатки или обороты. Чтение обычных записей регистра через `query` не заменяет вычисление виртуальной таблицы сервером.

## Поддерживаемые таблицы

| TABLE | Вид EntitySet | Функция | Периоды |
| --- | --- | --- | --- |
| `slice-last` | `InformationRegister_*` | `SliceLast` | `--period` |
| `slice-first` | `InformationRegister_*` | `SliceFirst` | `--period` |
| `balance` | `AccumulationRegister_*` | `Balance` | `--period` |
| `turnovers` | `AccumulationRegister_*` | `Turnovers` | `--start-period`, `--end-period` |
| `balance-and-turnovers` | `AccumulationRegister_*` | `BalanceAndTurnovers` | `--start-period`, `--end-period` |

Периоды передаются только если заданы. Пропущенные необязательные параметры обрабатывает сервер; параметры с `Nullable=false` нужно задать. Формат и тип значения определяются декларацией параметра в metadata, например `Edm.DateTime` принимает ISO datetime без смещения.

## Примеры

Все имена условные: сначала используйте `entities`, `describe` и `discover`, чтобы изучить фактическую публикацию.

```bash
python3 scripts/onec_odata.py register InformationRegister_КурсыВалют slice-last --period 2026-10-23T12:00:00 --condition '{"op":"eq","field":"Валюта_Key","value":"11111111-1111-4111-8111-111111111111"}' --limit 100
python3 scripts/onec_odata.py register AccumulationRegister_ТоварыНаСкладах balance --period 2026-10-23T12:00:00 --condition '{"op":"eq","field":"Склад_Key","value":"22222222-2222-4222-8222-222222222222"}'
python3 scripts/onec_odata.py register AccumulationRegister_ТоварыНаСкладах turnovers --start-period 2026-10-01T00:00:00 --end-period 2026-11-01T00:00:00
```

Команды выполняются из каталога skill. `--condition` принимает то же JSON-дерево, что `query --filter`, и проверяет поля по схеме исходного регистра. Полученное выражение передаётся строковым параметром `Condition` функции, с экранированием вложенных литералов. Это условие расчёта виртуальной таблицы, а не `$filter` её результата; результаты не проверяются как исходные записи регистра.

`--select` выбирает физические поля **возвращаемого типа функции**, а не поля исходного регистра. Их можно найти в `discover`: функция содержит `ReturnType`, а определения типов — в `metadata.types`. `--limit` — от 1 до 10000, по умолчанию 100; серверные ссылки продолжения обрабатываются в пределах этого лимита. Ответ: `{"ok":true,"items":[...]}`.

## Проверки публикации

Для вызова нужна однозначная `FunctionImport` с соответствующим именем и `IsBindable=true`. Первый параметр должен иметь тип коллекции сущностей выбранного EntitySet. Функция должна быть объявлена как операция без побочных эффектов (`IsSideEffecting=false`) либо иметь явный HTTP-метод GET; явные POST и `IsSideEffecting=true` запрещены. Возвращаемая коллекция должна ссылаться на известный EntityType/ComplexType.

Клиент использует объявленные параметры и возвращаемый тип. Если функции нет, её привязка неоднозначна, схема результата отсутствует или вид регистра не подходит, возвращается `operation_not_supported`. Клиент не угадывает endpoint и не подменяет запрос выгрузкой движений. Регистры бухгалтерии, вычислительные регистры, произвольные функции и действия в эту команду не входят.

Значения периодов и границы интервала интерпретирует 1С; команда не меняет время, не добавляет часовой пояс и не пересчитывает остатки самостоятельно. Совместимость конкретной публикации необходимо подтвердить проверкой чтения. Автономные тесты используют искусственные metadata и HTTP-ответы.

## Источники

- [1С: стандартный REST-интерфейс](https://1c-dn.com/1c_enterprise/rest_interface/).
- [1С: функции независимых регистров сведений](https://kb.1ci.com/1C_Enterprise_Platform/Guides/Developer_Guides/1C_Enterprise_8.3.23_Developer_Guide/Appendix_11._Describing_entities_provided_via_the_standard_OData_interface/11.9._Information_registers/11.9.1._Independent/?language=en).
- [OData 3.0 CSDL: FunctionImport](https://www.odata.org/documentation/odata-version-3-0/common-schema-definition-language-csdl/).
- [python_1c_odata: примеры и тесты регистров](https://github.com/itsuppartem/python_1c_odata/blob/a480c3f22ec8e1ef62018f97c683a3342d7f273b/tests/test_registers.py) — источник идей; библиотека не является зависимостью skill.
