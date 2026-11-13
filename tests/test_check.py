"""python -m hub check читает МойСклад и не пишет в него.

Плановый daily_upd тоже сверяет шаблон: иначе опечатка всплывает только в 17:00.
"""
import pytest

from hub.__main__ import cmd_check
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
