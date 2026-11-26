"""Коды статуса FBS, как они записаны в описании Seller API.

Источник на 26.11.2026: зеркало OpenAPI операции PostingAPI_GetFbsPostingListV3
(сервер https://api-seller.ozon.ru). Сам docs.ozon.ru из этой среды отдал
бесконечный HTTP 307, поэтому коды не дополнялись по памяти.

Два списка в одном описании не совпадают: у фильтра есть ``delivered``,
у поля ``status`` отправления его нет, зато есть ``awaiting_verification``
и ``cancelled_from_split_pending``. Импорт сохраняет строку статуса как есть
и не отвергает код, которого нет в этих списках.
"""

# filter.status метода списка v3. У списка v4 то же поле называется filter.statuses.
FILTER_STATUSES: tuple[str, ...] = (
    "awaiting_registration",
    "acceptance_in_progress",
    "awaiting_approve",
    "awaiting_packaging",
    "awaiting_deliver",
    "arbitration",
    "client_arbitration",
    "delivering",
    "driver_pickup",
    "delivered",
    "cancelled",
    "not_accepted",
    "sent_by_seller",
)

# posting.status в схеме v3FbsPosting того же описания.
OBJECT_STATUSES: tuple[str, ...] = (
    "acceptance_in_progress",
    "arbitration",
    "awaiting_approve",
    "awaiting_deliver",
    "awaiting_packaging",
    "awaiting_registration",
    "awaiting_verification",
    "cancelled",
    "cancelled_from_split_pending",
    "client_arbitration",
    "delivering",
    "driver_pickup",
    "not_accepted",
    "sent_by_seller",
)

# posting.substatus. Повтор «posting_created» в тексте описания схлопнут.
SUBSTATUSES: tuple[str, ...] = (
    "posting_acceptance_in_progress",
    "posting_in_arbitration",
    "posting_created",
    "posting_in_carriage",
    "posting_not_in_carriage",
    "posting_registered",
    "posting_transferring_to_delivery",
    "posting_awaiting_passport_data",
    "posting_awaiting_registration",
    "posting_registration_error",
    "posting_split_pending",
    "posting_canceled",
    "posting_in_client_arbitration",
    "posting_delivered",
    "posting_received",
    "posting_conditionally_delivered",
    "posting_in_courier_service",
    "posting_in_pickup_point",
    "posting_on_way_to_city",
    "posting_on_way_to_pickup_point",
    "posting_returned_to_warehouse",
    "posting_transferred_to_courier_service",
    "posting_driver_pick_up",
    "posting_not_in_sort_center",
    "sent_by_seller",
)
