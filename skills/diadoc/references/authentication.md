# Получение секретов Контур.Диадока

1. Подготовь техническую учётную запись с доступом к нужным ящикам; для черновиков нужны права создания и редактирования.
2. Если приложения нет, [подключи API](https://www.diadoc.ru/integrations/api). В [Кабинете интегратора](https://integrations.kontur.ru/) получи ID приложения и выпусти API-ключ.
3. Установи `clientMode=AuthorizationCode`. В разделе «Выпуск токенов» выбери `openid`, `profile`, `email`, `offline_access`, `Diadoc.PublicAPI`.
4. Нажми «Получить refresh-токен», войди технической учётной записью, подтверди доступ и введи API-ключ. Сохрани выданный refresh token.

| Параметр | Где взять |
|---|---|
| `DIADOC_CLIENT_ID` | ID приложения (`client_id`) в Кабинете интегратора. |
| `DIADOC_CLIENT_SECRET` | API-ключ приложения (`client_secret`). |
| `DIADOC_REFRESH_TOKEN` | Refresh token (`refresh_token`), полученный на шаге 4. |

Используй refresh token, выпущенный в Кабинете интегратора для Refresh Token Flow. Access token CLI получает самостоятельно.

Не передавай секреты в чат и не сохраняй их в репозитории.

Справка Контура: [авторизация и обновление токенов](https://developer.kontur.ru/doc/diadoc-api/authentication.html).
