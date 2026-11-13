"""python -m hub check читает МойСклад и не пишет в него.

Плановый daily_upd тоже сверяет шаблон: иначе опечатка всплывает только в 17:00.
Вебхуки административные: 30004 или HTTP 403 на GET /entity/webhook — SKIPPED.
"""
import pytest

from hub.__main__ import cmd_check, main
from hub.moysklad import MoySklad
from hub.settings import Settings

WRITES = ("POST", "PUT", "DELETE", "PATCH")


def settings_for(automations: dict) -> Settings:
    return Settings(ms_token="test-token", dry_run=True, api_key="k", webhook_secret="s3cret",
                    public_url="https://hub.example.ru",
                    database_url="sqlite+aiosqlite:///:memory:", automations=automations)


@pytest.fixture
def checked(fake, monkeypatch):
    monkeypatch.setattr("hub.__main__.client",
                        lambda s: MoySklad(s.ms_token, transport=fake.transport()))
    return fake


async def test_check_scheduled_template_is_read_only(checked, capsys):
    code = await cmd_check(settings_for({"daily_upd": {"template": "УПД"}}))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "шаблон «УПД»" in out and "HUB_DRY_RUN" in out and "Готово к работе" in out
    assert [method for method, _ in checked.calls if method in WRITES] == []


async def test_check_unknown_scheduled_template_fails(checked, capsys):
    code = await cmd_check(settings_for({"daily_upd": {"template": "Акт"}}))
    out = capsys.readouterr().out
    assert code == 1, out
    assert "Акт" in out
    assert [method for method, _ in checked.calls if method in WRITES] == []


async def test_check_skips_disabled_and_still_checks_states(checked, capsys):
    off = await cmd_check(settings_for({"daily_upd": {"enabled": False, "template": "Акт"}}))
    assert off == 0, capsys.readouterr().out
    code = await cmd_check(settings_for({"sync_order_state": {"state": "Нет такого"}}))
    out = capsys.readouterr().out
    assert code == 1 and "Нет такого" in out


def _daily():
    return settings_for({"daily_upd": {"template": "УПД"}})


@pytest.mark.parametrize(("status", "code", "text"), [
    (403, 30004, "Необходимы права администратора для работы с webhook"),
    (403, 1016, "Доступ запрещен: у вас нет прав на просмотр данного объекта"),
    (400, 30004, "Необходимы права администратора для работы с webhook"),
])
async def test_webhook_without_admin_is_skipped(checked, capsys, status, code, text):
    checked.http_errors["entity/webhook"] = (status, text, code)
    exit_code = await cmd_check(_daily())
    out = capsys.readouterr().out
    assert exit_code == 0, out
    assert "SKIPPED" in out and "не успешная проверка" in out and "расширять права" in out
    assert str(code) in out and "GET /entity/webhook" in out
    assert "GET /entity/webhook доступен" not in out and "ведут сюда" not in out
    assert "шаблон «УПД»" in out and "чтение отгрузок доступно" in out
    assert ("GET", "entity/webhook") in checked.calls
    assert [method for method, _ in checked.calls if method in WRITES] == []


async def test_missing_upd_template_fails_even_if_webhooks_skipped(checked, capsys):
    checked.template_names["demand"] = [
        name for name in checked.template_names["demand"] if name.casefold() != "упд"]
    checked.http_errors["entity/webhook"] = (
        403, "Необходимы права администратора для работы с webhook", 30004)
    exit_code = await cmd_check(_daily())
    out = capsys.readouterr().out
    assert exit_code == 1, out
    assert "нет шаблона печати «УПД»" in out and "SKIPPED" in out


async def test_template_api_error_fails_even_if_webhooks_skipped(checked, capsys):
    checked.http_errors["entity/demand/metadata/embeddedtemplate"] = (
        400, "Ошибка в адресе запроса", 1038)
    checked.http_errors["entity/webhook"] = (
        403, "Необходимы права администратора для работы с webhook", 30004)
    exit_code = await cmd_check(_daily())
    out = capsys.readouterr().out
    assert exit_code == 1, out
    assert "Ошибка в адресе запроса" in out and "SKIPPED" in out
    assert [method for method, _ in checked.calls if method in WRITES] == []


async def test_demand_forbidden_is_mandatory_even_if_webhooks_skipped(checked, capsys):
    checked.http_errors["entity/demand"] = (
        403, "Доступ запрещен: у вас нет прав на просмотр данного объекта", 1016)
    checked.http_errors["entity/webhook"] = (
        403, "Необходимы права администратора для работы с webhook", 30004)
    exit_code = await cmd_check(_daily())
    out = capsys.readouterr().out
    assert exit_code == 1, out
    assert "нет чтения отгрузок" in out and "SKIPPED" in out
    assert "Готово к работе" not in out


async def test_webhook_other_api_error_is_not_skip(checked, capsys):
    checked.http_errors["entity/webhook"] = (400, "Ошибка в адресе запроса", 1038)
    exit_code = await cmd_check(_daily())
    out = capsys.readouterr().out
    assert exit_code == 1, out
    assert "SKIPPED" not in out and "Ошибка в адресе запроса" in out


def test_check_without_token_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("MOYSKLAD_TOKEN", "")
    exit_code = main(["check"])
    out = capsys.readouterr().out
    assert exit_code == 1, out
    assert "не задан MOYSKLAD_TOKEN" in out
