# Статусы FBS. Сверка 26.11.2026

Источник кодов: зеркало OpenAPI операции `PostingAPI_GetFbsPostingListV3`, сервер `https://api-seller.ozon.ru`. Официальный `docs.ozon.ru` в этот день отдал бесконечный HTTP 307.

В одном описании списки расходятся. У фильтра есть `delivered`, у поля `status` его нет, зато есть `awaiting_verification` и `cancelled_from_split_pending`. Импорт сохраняет любую непустую строку и не отвергает неизвестный код.

Подстатус `posting_canceled` записан с одной `l`, как в источнике.

Русские подписи ниже — предложение для будущей карты статусов МойСклад. Справка TopSeller коды API не публикует. Пока владелец не подтвердит имена статусов в своём МойСклад, хаб их не создаёт и не назначает.

Статус маршрутного листа «ПОДТВЕРЖДЕН / ОЖИДАЕТ СБОРКИ» для Ozon не используется: его уже смотрит `logistics_sheet`.

`delivered` и `posting_delivered` не означают выплату. Платёж по ним не создаётся.

## filter.statuses

| Код | Предложение, не подтверждено |
|---|---|
| `awaiting_registration` | ожидает регистрации |
| `acceptance_in_progress` | идёт приёмка |
| `awaiting_approve` | ожидает подтверждения |
| `awaiting_packaging` | ожидает сборки |
| `awaiting_deliver` | ожидает отгрузки |
| `arbitration` | арбитраж |
| `client_arbitration` | арбитраж покупателя |
| `delivering` | доставляется |
| `driver_pickup` | у водителя |
| `delivered` | доставлен, без платежа |
| `cancelled` | отменён |
| `not_accepted` | не принят |
| `sent_by_seller` | отправлен продавцом |

## posting.status

| Код | Предложение, не подтверждено |
|---|---|
| `acceptance_in_progress` | идёт приёмка |
| `arbitration` | арбитраж |
| `awaiting_approve` | ожидает подтверждения |
| `awaiting_deliver` | ожидает отгрузки |
| `awaiting_packaging` | ожидает сборки |
| `awaiting_registration` | ожидает регистрации |
| `awaiting_verification` | на проверке |
| `cancelled` | отменён |
| `cancelled_from_split_pending` | отмена из ожидающего разделения |
| `client_arbitration` | арбитраж покупателя |
| `delivering` | доставляется |
| `driver_pickup` | у водителя |
| `not_accepted` | не принят |
| `sent_by_seller` | отправлен продавцом |

## posting.substatus

Хранятся как есть. В статус МойСклад на этом этапе не переводятся.

`posting_acceptance_in_progress`, `posting_in_arbitration`, `posting_created`, `posting_in_carriage`, `posting_not_in_carriage`, `posting_registered`, `posting_transferring_to_delivery`, `posting_awaiting_passport_data`, `posting_awaiting_registration`, `posting_registration_error`, `posting_split_pending`, `posting_canceled`, `posting_in_client_arbitration`, `posting_delivered`, `posting_received`, `posting_conditionally_delivered`, `posting_in_courier_service`, `posting_in_pickup_point`, `posting_on_way_to_city`, `posting_on_way_to_pickup_point`, `posting_returned_to_warehouse`, `posting_transferred_to_courier_service`, `posting_driver_pick_up`, `posting_not_in_sort_center`, `sent_by_seller`.
