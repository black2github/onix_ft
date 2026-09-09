# onix_ft/tests/test_selenium_missing.py
#
# Инцидент 2026-09-08 (закрытый контур): при запуске run_receiver пользователь
# получал `NameError: name 'By' is not defined` — бессмысленное сообщение,
# за которым пряталась настоящая причина: selenium не импортируется в активном
# venv. Импорт selenium обёрнут в try/except, провал глотался, а селекторы на
# уровне модуля обращались к неопределённому `By`.
#
# Здесь закреплено: без selenium модуль импортируется (чистые помощники
# работают), а попытка поднять транспорт падает с ПОНЯТНЫМ сообщением —
# причиной и шагами INSTALL.txt. Пара на НЕсрабатывание: с установленным
# selenium ничего не меняется.
#
# Отсутствие selenium моделируется в подпроцессе: sys.modules["selenium"] = None
# заставляет любой `import selenium...` падать с ImportError.

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROBE = r"""
import sys
sys.modules["selenium"] = None                     # selenium «не установлен»
from onix_ft.transport import selenium_driver as sd
print("available:", sd.SELENIUM_AVAILABLE)
print("selectors:", sd.SEL_INPUT_BOX)
print("feed:", isinstance(sd.feed_record(1, "el", False, 1, "текст"), dict))
try:
    sd.OnixSeleniumTransport()
except ImportError as e:
    print("error:", str(e).replace("\n", " | "))
"""


def _run_without_selenium():
    return subprocess.run([sys.executable, "-c", PROBE], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8")


def test_module_imports_without_selenium():
    r = _run_without_selenium()
    assert r.returncode == 0, r.stderr
    assert "available: False" in r.stdout
    assert "selectors: ('css selector', '.slate-message-input')" in r.stdout
    assert "feed: True" in r.stdout                # чистые помощники живы


def test_transport_fails_with_clear_message():
    r = _run_without_selenium()
    out = r.stdout
    assert "error:" in out
    assert "selenium не импортируется" in out
    assert "INSTALL.txt" in out and "pip install --no-index" in out
    assert "ImportError" in out or "ModuleNotFoundError" in out   # исходная причина названа
    assert "NameError" not in r.stderr               # старого симптома нет


def test_nothing_changes_when_selenium_present():
    """Пара на НЕсрабатывание: с установленным selenium — штатный путь."""
    from onix_ft.transport import selenium_driver as sd
    assert sd.SELENIUM_AVAILABLE is True
    assert sd.SELENIUM_IMPORT_ERROR == ""
    from selenium.webdriver.common.by import By
    assert sd.SEL_INPUT_BOX == (By.CSS_SELECTOR, ".slate-message-input")
