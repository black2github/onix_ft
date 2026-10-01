# onix_ft/tests/test_chat_identity.py
#
# Один чат для обмена и очистки (2026-10-01). Отправка и чтение работают с
# чатом, открытым в браузере (ONIX_CHAT_URL); очистка — периодическая и по
# --clear-history — ищет чат в списке по имени ONIX_CHAT_NAME. Настройки не
# сверялись: при изменённом URL и имени по умолчанию «Сохраненные сообщения»
# передача шла в одном чате, а каждые N блоков чистилась история другого
# (вопрос владельца по пакету onix_offline).
#
# Закреплено: при старте открытый чат сверяется с ONIX_CHAT_NAME — расхождение
# стоп, совпадение тихо, список не виден — предупреждение; чат в списке ищется
# по ТОЧНОМУ имени, не по подстроке. Selenium не нужен: драйвер подменяется.

import logging
from types import SimpleNamespace

import pytest

from onix_ft import config
from onix_ft.transport import selenium_driver as sd


class _El:
    def __init__(self, text):
        self.text = text


class _Driver:
    """Заглушка WebDriver: отдаёт заданные элементы на любой find_elements."""
    def __init__(self, elements):
        self._elements = elements

    def find_elements(self, *_a, **_k):
        return self._elements


def _transport(elements):
    t = sd.OnixSeleniumTransport.__new__(sd.OnixSeleniumTransport)
    t._driver = _Driver(elements)
    return t


@pytest.fixture
def chat_name(monkeypatch):
    monkeypatch.setattr(config, "ONIX_CHAT_NAME", "Канал переноса")
    return "Канал переноса"


class TestActiveChat:
    def test_same_chat_is_silent(self, chat_name, caplog):
        t = _transport([_El(" Канал переноса ")])
        with caplog.at_level(logging.WARNING):
            t.check_active_chat()
        assert not caplog.records

    def test_other_chat_stops_with_both_names(self, chat_name):
        t = _transport([_El("Сохраненные сообщения")])
        with pytest.raises(RuntimeError) as e:
            t.check_active_chat()
        msg = str(e.value)
        assert "Сохраненные сообщения" in msg and "Канал переноса" in msg
        assert "ONIX_CHAT_NAME" in msg

    def test_hidden_list_only_warns(self, chat_name, caplog):
        # НЕсрабатывание стопа: список чатов не виден — сверить нечем
        t = _transport([])
        with caplog.at_level(logging.WARNING):
            t.check_active_chat()
        assert any("определить не удалось" in r.getMessage() for r in caplog.records)

    def test_active_name_skips_empty_elements(self):
        t = _transport([_El(""), _El("  "), _El("Канал переноса")])
        assert t.active_chat_name() == "Канал переноса"


class TestExactName:
    def test_selector_matches_exact_name_only(self):
        xpath = sd.SEL_CHAT_BUTTON_TMPL.format(name="Канал переноса")
        assert "normalize-space(.)='Канал переноса'" in xpath
        assert "contains(text()" not in xpath


class TestVersion:
    def test_version_is_declared(self):
        from onix_ft import __version__
        parts = __version__.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)
