# Контракт Seller API, зафиксированный 26.11.2026

`docs.ozon.ru` из среды сборки отвечал HTTP 307 по кругу (`?__rr=1`, `2`, …). HTML официальной документации не прочитан. Ниже только то, что подтверждено другими источниками. Неподтверждённые методы не вызываются.

## Список FBS. Вызывается

`POST https://api-seller.ozon.ru/v4/posting/fbs/list`

- `POST /v3/posting/fbs/list` отключён 01.06.2026. Дата есть в разделе документации от 30.04.2026 и в [gam6itko/ozon-seller#115](https://github.com/gam6itko/ozon-seller/issues/115).
- Ответ v4 не лежит в `result`. На верхнем уровне: `postings`, `has_next`, `cursor`. Живое сравнение с v3 от 22.08.2026: [DeviceIngineering/ozon-mcp-server#6](https://github.com/DeviceIngineering/ozon-mcp-server/issues/6). Конверт v3 клиент не считает пустым списком.
- Тело запроса по `FbsService::list` / операция `PostingFbsList` в gam6itko: `cursor`, `filter`, `limit`, `sort_dir`, `translit`, `with`.
- `limit` — целое 1–100, период `since`–`to` не длиннее года (заметка packagist `vasyag07/ozon-seller`). Клиент шлёт `limit: 100` и `sort_dir: ASC`. Поля `offset` и `dir` не шлёт.
- `filter.statuses` не передаётся: за окно нужны все статусы.
- `with.analytics_data`, `barcodes`, `financial_data`, `legal_info` все `false`.

Заголовки: `Client-Id`, `Api-Key`. Редирект не принимается, чтобы ключ не ушёл на другой хост. Текст ошибки длиннее 500 символов обрезается, значение ключа из него вырезается.

Пагинация: пустой `cursor` на первой странице. Следующая страница запрашивается только если `has_next` истинно и новый `cursor` не пустой и не повторяет уже виденный. Иначе импорт останавливается с ошибкой контракта. За один запуск не больше 20 000 отправлений.

## Поля отправления, которые сохраняются

Схема полей взята из зеркала OpenAPI операции `PostingAPI_GetFbsPostingListV3` (сервер `api-seller.ozon.ru`). Список v4 на живом кабинете вернул те же отправления, что v3. Поэтому поля объекта приняты из этого зеркала, а не выдуманы.

Сохраняются: `posting_number`, `order_id`, `order_number`, `status`, `substatus`, `tpl_integration_type`, `in_process_at`, `shipment_date`, `delivering_date`, `parent_posting_number`, `is_express`, `delivery_method.warehouse_id`, `delivery_method.warehouse`, `cancellation.cancel_reason_id`, `cancellation.cancellation_type`, `cancellation.cancelled_after_ship`.

По строке: `offer_id`, `sku`, `quantity`, `price` (строка), `currency_code`. Не больше 500 строк. `quantity` — целое неотрицательное, не bool. Цена не приводится к float.

Не сохраняются: покупатель, адрес, телефон, имя товара, `financial_data`, штрихкоды, `legal_info`, текст причины отмены.

`offer_id` для фильтра МойСклад должен совпасть с `^[\w.+/() -]{1,255}$`. Пустой и небезопасный (`=`, `;`, `,`) пропускаются, запрос в МойСклад по ним не уходит. Язык фильтра МойСклад использует `;` как И.

## Методы, которые этим срезом не вызываются

| Метод | Почему не зафиксирован |
|---|---|
| `POST /v4/posting/fbs/unfulfilled/list` | в снимке swagger phpsoftbox от 28.09.2026 путь ещё есть; для импорта не нужен |
| `POST /v3/posting/fbs/get` | путь есть в том же снимке; список уже отдаёт поля среза |
| `POST /v3/posting/fbo/list` | `/v2/posting/fbo/list` отключён 31.08.2026. Замена названа в issue #6, тело запроса v3 не зафиксировано |
| возвраты, отмена, сборка, перевозка | не сверялись. `POST /v1/carriage/create` с телом `{}` на живом кабинете создаёт отгрузку (issue #6). Мутирующие методы не пробуем |
| остатки и цены | отдельное согласование, в этом срезе выключены |
| подписки Push | текущие подписки не меняем |

Снимок swagger phpsoftbox от 28.09.2026, SHA-256 `822ed6f96499ab2f1865e714a97673da25d8f3f4ed29390194cf241891ccd112`, подтверждает наличие `/v4/posting/fbs/list` и появление `postingFboV3` 14.09.2026. Это не замена чтения официальной схемы.
