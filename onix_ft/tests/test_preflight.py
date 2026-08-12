"""
Тесты предполётных проверок запуска браузера (без браузера и без драйвера).

Запуск:
    python -m pytest onix_ft/tests/test_preflight.py -v
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft.transport import preflight, selenium_driver
from onix_ft.transport.preflight import PreflightError


# ── развёртывание путей ──────────────────────────────────────────────────────

def test_expand_path_expands_env_vars(monkeypatch):
    monkeypatch.setenv("ONIXFT_TEST_ROOT", r"C:\onix-test")
    result = preflight.expand_path(r"%ONIXFT_TEST_ROOT%\profile")
    assert result == Path(r"C:\onix-test\profile")


def test_expand_path_empty_means_not_configured():
    assert preflight.expand_path("") is None
    assert preflight.expand_path(None) is None
    assert preflight.expand_path("   ") is None


def test_expand_path_keeps_plain_path():
    assert preflight.expand_path(r"C:\onix\profile") == Path(r"C:\onix\profile")


# ── чужой профиль пользователя (корневая причина инцидента) ──────────────────

def test_foreign_user_dir_detects_other_user(monkeypatch):
    monkeypatch.setattr(preflight, "current_user", lambda: "gpbu20890")
    owner = preflight.foreign_user_dir(
        Path(r"C:\Users\gpbu33430\AppData\Local\Google\Chrome\OnixFT")
    )
    assert owner == "gpbu33430"


def test_foreign_user_dir_ignores_own_profile(monkeypatch):
    monkeypatch.setattr(preflight, "current_user", lambda: "gpbu20890")
    own = Path(r"C:\Users\gpbu20890\AppData\Local\Google\Chrome\OnixFT")
    assert preflight.foreign_user_dir(own) is None


def test_foreign_user_dir_case_insensitive(monkeypatch):
    monkeypatch.setattr(preflight, "current_user", lambda: "GPBU20890")
    own = Path(r"C:\Users\gpbu20890\AppData\Local\Google\Chrome\OnixFT")
    assert preflight.foreign_user_dir(own) is None


def test_foreign_user_dir_ignores_paths_outside_users(monkeypatch):
    monkeypatch.setattr(preflight, "current_user", lambda: "gpbu20890")
    assert preflight.foreign_user_dir(Path(r"C:\onix\profile-onixft")) is None


# ── доступность каталога профиля ─────────────────────────────────────────────

def test_ensure_profile_dir_creates_missing(tmp_path):
    target = tmp_path / "profile" / "OnixFT"
    preflight.ensure_profile_dir(target)
    assert target.is_dir()
    # Проба записи убирается за собой — мусор в профиле не остаётся
    assert list(target.iterdir()) == []


def test_ensure_profile_dir_reports_foreign_user_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "current_user", lambda: "gpbu20890")

    def deny(*_args, **_kwargs):
        raise PermissionError("[WinError 5] Отказано в доступе")

    monkeypatch.setattr(Path, "mkdir", deny)
    foreign = Path(r"C:\Users\gpbu33430\AppData\Local\Google\Chrome\OnixFT")
    with pytest.raises(PreflightError) as err:
        preflight.ensure_profile_dir(foreign)

    message = str(err.value)
    assert "gpbu33430" in message          # назван владелец каталога
    assert "gpbu20890" in message          # и текущий пользователь
    assert "BROWSER_PROFILE_DIR" in message  # сказано, что править


# ── сетевой путь ─────────────────────────────────────────────────────────────

def test_is_network_path_detects_unc():
    assert preflight.is_network_path(Path(r"\\vdi-share\profiles\onix")) is True


def test_is_network_path_local_drive_is_not_network(tmp_path):
    assert preflight.is_network_path(tmp_path) is False


# ── версии ───────────────────────────────────────────────────────────────────

def test_parse_major():
    assert preflight.parse_major("147.0.7727.117") == 147
    assert preflight.parse_major("ChromeDriver 150.0.7871.46 (5b586c0)") == 150
    assert preflight.parse_major("") is None
    assert preflight.parse_major(None) is None
    assert preflight.parse_major("не определена") is None


def test_version_conflict_reports_mismatch():
    message = preflight.version_conflict("147.0.7727.117", "150.0.7871.46")
    assert message is not None
    assert "147" in message and "150" in message
    assert "CHROMEDRIVER_PATH" in message


def test_version_conflict_silent_when_majors_match():
    assert preflight.version_conflict("147.0.7727.117", "147.0.7727.85") is None


def test_version_conflict_silent_when_version_unknown():
    """Неполные данные не повод блокировать запуск (асимметрия ошибок)."""
    assert preflight.version_conflict(None, "147.0.7727.117") is None
    assert preflight.version_conflict("147.0.7727.117", None) is None
    assert preflight.version_conflict(None, None) is None


# ── занятость профиля ────────────────────────────────────────────────────────

def test_profile_from_command_line_quoted_and_plain():
    quoted = r'chrome.exe --user-data-dir="C:\onix\profile onixft" --no-sandbox'
    assert preflight._profile_from_command_line(quoted) == r"C:\onix\profile onixft"

    plain = r"chrome.exe --user-data-dir=C:\onix\profile --no-sandbox"
    assert preflight._profile_from_command_line(plain) == r"C:\onix\profile"

    assert preflight._profile_from_command_line("chrome.exe --no-sandbox") is None


def test_same_path_normalizes_case_and_separators():
    assert preflight._same_path(r"C:\Onix\Profile\\", r"c:/onix/profile") is True
    assert preflight._same_path(r"C:\onix\profile", r"C:\onix\other") is False


def test_check_profile_free_raises_when_busy(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "processes_using_profile", lambda *_a, **_k: [4242])
    with pytest.raises(PreflightError) as err:
        preflight.check_profile_free(tmp_path)
    assert "4242" in str(err.value)


def test_check_profile_free_passes_when_unknown(monkeypatch, tmp_path):
    """Проверка не сработала → запуск НЕ блокируем (лучше попытка, чем отказ)."""
    monkeypatch.setattr(preflight, "processes_using_profile", lambda *_a, **_k: None)
    preflight.check_profile_free(tmp_path)


def test_check_profile_free_passes_when_free(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "processes_using_profile", lambda *_a, **_k: [])
    preflight.check_profile_free(tmp_path)


def test_processes_using_profile_skips_query_without_browser(monkeypatch, tmp_path):
    """Быстрый фильтр: браузера в системе нет — тяжёлый CIM-запрос не нужен."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(preflight, "any_browser_running", lambda *_a, **_k: False)

    def fail(*_args, **_kwargs):
        raise AssertionError("CIM-запрос не должен выполняться")

    monkeypatch.setattr(preflight.subprocess, "run", fail)
    assert preflight.processes_using_profile(tmp_path) == []


# ── подсказка при сорванном старте ───────────────────────────────────────────

def test_session_failure_hint_mentions_key_suspects():
    hint = preflight.session_failure_hint(Path(r"C:\onix\profile"), r"C:\onix\chromedriver.exe")
    assert "профил" in hint.lower()
    assert "DRIVER_LOG_PATH" in hint  # без журнала — подсказка его включить


def test_session_failure_hint_points_to_existing_log(tmp_path):
    log = tmp_path / "cd.log"
    log.write_text("[INFO]: обычный журнал без признаков угона", encoding="utf-8")
    hint = preflight.session_failure_hint(
        Path(r"C:\onix\profile"), r"C:\onix\chromedriver.exe", log
    )
    assert str(log) in hint


# ── политика домена: запрет средств разработчика ─────────────────────────────

def test_describe_devtools_policy_blocks_only_on_value_2():
    message = preflight.describe_devtools_policy(2)
    assert message is not None
    assert "DeveloperToolsAvailability=2" in message
    assert "chrome://policy" in message


def test_describe_devtools_policy_silent_on_allowing_values():
    """0/1 разрешают отладку, отсутствие значения ничего не доказывает."""
    assert preflight.describe_devtools_policy(0) is None
    assert preflight.describe_devtools_policy(1) is None
    assert preflight.describe_devtools_policy(None) is None


def test_read_chrome_policy_survives_absent_key():
    """Политика не задана — молча None, без исключения."""
    assert preflight.read_chrome_policy("НетТакойПолитики") is None


def test_debug_session_refused_names_policy_when_registry_shows_it(tmp_path, monkeypatch):
    log = tmp_path / "driver.log"
    log.write_text(REFUSED_LOG, encoding="utf-8")
    monkeypatch.setattr(preflight, "read_chrome_policy", lambda *_a, **_k: 2)
    message = preflight.debug_session_refused(log)
    assert "DeveloperToolsAvailability=2" in message


# ── браузер не отдал отладочную сессию ───────────────────────────────────────

REFUSED_LOG = """\
[1786462545.894][INFO]: Starting ChromeDriver 149.0.7827.201 (6a7b3dbec3) on port 53299
[1786462546.429][INFO]: Populating Preferences file: {
   "default_search_provider": {
      "url": "https://sfera/SitePages/Search.aspx?q={searchTerms}"
   }
}
[1786462547.554][DEBUG]: DevTools HTTP Response: {
   "Browser": "Chrome/149.0.7827.103"
}
[1786462547.572][DEBUG]: DevTools WebSocket Event: Target.attachedToTarget {
   "targetInfo": {
      "type": "tab",
      "url": "https://webexp.msg.gpb.ru/#/"
   }
}
[1786462547.572][DEBUG]: DevTools WebSocket Command: Target.setAutoAttach (id=3)
[1786462547.572][DEBUG]: DevTools WebSocket Response:  (id=3) browser \
{"code":-32001,"message":"Session with given id not found."}
"""


def test_debug_session_refused_names_page_and_versions(tmp_path):
    log = tmp_path / "driver.log"
    log.write_text(REFUSED_LOG, encoding="utf-8")
    message = preflight.debug_session_refused(log)
    assert message is not None
    # Названа именно вкладка, а не поисковик из дампа Preferences
    assert "открыл https://webexp.msg.gpb.ru/#/ " in message
    assert "searchTerms" not in message
    # Обе версии из журнала — по ним сверяют пару браузер/драйвер
    assert "149.0.7827.103" in message and "149.0.7827.201" in message
    # Направления проверки: политика домена и запасной канал
    assert "chrome://policy" in message
    assert "USE_DEBUG_PIPE" in message


def test_debug_session_refused_does_not_blame_the_page(tmp_path):
    """Отказ не зависит от страницы — прежняя формулировка уводила не туда."""
    log = tmp_path / "driver.log"
    log.write_text(REFUSED_LOG, encoding="utf-8")
    message = preflight.debug_session_refused(log)
    assert "ни при чём" in message
    assert "OPEN_IN_APP_WINDOW" not in message


def test_debug_session_refused_silent_on_other_logs(tmp_path):
    log = tmp_path / "driver.log"
    log.write_text("[INFO]: Starting ChromeDriver\n[INFO]: RESPONSE InitSession", encoding="utf-8")
    assert preflight.debug_session_refused(log) is None


def test_debug_session_refused_survives_missing_log(tmp_path):
    assert preflight.debug_session_refused(None) is None
    assert preflight.debug_session_refused(tmp_path / "нет-такого.log") is None


def test_session_failure_hint_replaces_checklist_when_cause_known(tmp_path):
    """Причина установлена — общий чек-лист не показываем, чтобы не путать."""
    log = tmp_path / "driver.log"
    log.write_text(REFUSED_LOG, encoding="utf-8")
    hint = preflight.session_failure_hint(Path(r"C:\onix\profile"), None, log)
    assert "USE_DEBUG_PIPE" in hint
    assert "Что проверить по порядку" not in hint


# ── ключи запуска браузера ───────────────────────────────────────────────────

def test_browser_arguments_without_app_url():
    args = selenium_driver.browser_arguments(Path(r"C:\onix\profile"))
    assert r"--user-data-dir=C:\onix\profile" in args
    assert not any(a.startswith("--app=") for a in args)


def test_browser_arguments_with_app_url():
    args = selenium_driver.browser_arguments(Path(r"C:\onix\profile"), "https://webexp.msg.gpb.ru/#/")
    assert "--app=https://webexp.msg.gpb.ru/#/" in args


def test_browser_arguments_without_profile():
    args = selenium_driver.browser_arguments(None)
    assert not any(a.startswith("--user-data-dir") for a in args)
    assert "--no-sandbox" in args


def test_browser_arguments_debug_pipe_off_by_default():
    assert "--remote-debugging-pipe" not in selenium_driver.browser_arguments(None)


def test_browser_arguments_debug_pipe_on():
    args = selenium_driver.browser_arguments(None, None, debug_pipe=True)
    assert "--remote-debugging-pipe" in args


# ── снимок ленты чата (разбор потерянных блоков) ─────────────────────────────

def test_feed_record_keeps_diagnostic_fields():
    record = selenium_driver.feed_record(3, "el-42", seen=True, bubbles=2, text="##FT|v1|данные")
    assert record["i"] == 3 and record["id"] == "el-42"
    assert record["seen"] is True
    assert record["bubbles"] == 2          # склейка сообщений видна в снимке
    assert record["len"] == len("##FT|v1|данные")
    assert record["head"].startswith("##FT|v1|")


def test_feed_record_flattens_newlines_in_head():
    record = selenium_driver.feed_record(0, "el-1", seen=False, bubbles=1, text="строка1\nстрока2")
    assert "\n" not in record["head"]
    assert record["len"] == len("строка1\nстрока2")   # длина считается по оригиналу


def test_feed_record_survives_empty_text():
    record = selenium_driver.feed_record(0, "el-1", seen=False, bubbles=0, text="")
    assert record["len"] == 0 and record["head"] == ""


def test_append_feed_records_writes_jsonl(tmp_path):
    import json

    target = tmp_path / "вложенный" / "feed.jsonl"
    selenium_driver.append_feed_records(target, [
        selenium_driver.feed_record(0, "a", False, 1, "первое"),
        selenium_driver.feed_record(1, "b", True, 2, "второе"),
    ])
    selenium_driver.append_feed_records(target, [
        selenium_driver.feed_record(0, "c", False, 1, "третье"),
    ])

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3                       # дописывает, а не перезаписывает
    assert json.loads(lines[1])["bubbles"] == 2
    assert json.loads(lines[2])["head"] == "третье"


def test_append_feed_records_ignores_empty_batch(tmp_path):
    target = tmp_path / "feed.jsonl"
    selenium_driver.append_feed_records(target, [])
    assert not target.exists()


# ── единицы чтения ленты: склейка сообщений в одну строку ────────────────────

class FakeElement:
    """Подделка элемента браузера: ровно то, что использует _feed_units."""

    def __init__(self, element_id, text="", bubbles=None):
        self.id = element_id
        self.text = text
        self._bubbles = bubbles or []

    def find_elements(self, _by, _selector):
        return self._bubbles


def _transport():
    return selenium_driver.OnixSeleniumTransport()


def test_feed_units_splits_grouped_row_into_messages():
    """
    Регресс инцидента 2026-08-12: строка со склейкой двух сообщений давала
    одну единицу чтения, второе сообщение терялось молча (блок «пропадал»,
    приём срывался на NACK).
    """
    grouped = FakeElement("row-1", bubbles=[
        FakeElement("bubble-1", "##FT|блок-3##"),
        FakeElement("bubble-2", "##FT|блок-4##"),
    ])
    units = _transport()._feed_units([grouped])

    assert [key for key, _el, _is_bubble in units] == ["bubble-1", "bubble-2"]
    assert all(is_bubble for _key, _el, is_bubble in units)


def test_feed_units_falls_back_to_row_without_bubbles():
    """Служебная строка (разделитель дат) остаётся единицей сама по себе."""
    plain = FakeElement("row-2", "12 августа")
    units = _transport()._feed_units([plain])

    assert units == [("row-2", plain, False)]


def test_feed_units_keys_are_unique_across_rows():
    rows = [
        FakeElement("row-1", bubbles=[FakeElement("b1", "первое")]),
        FakeElement("row-2", bubbles=[FakeElement("b2", "второе"), FakeElement("b3", "третье")]),
    ]
    keys = [key for key, _el, _is_bubble in _transport()._feed_units(rows)]
    assert keys == ["b1", "b2", "b3"]
    assert len(set(keys)) == len(keys)


def test_extract_text_of_bubble_strips_timestamp():
    bubble = FakeElement("b1", "##FT|v1|данные##\n18:58")
    assert _transport()._extract_text(bubble, is_bubble=True) == "##FT|v1|данные##"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
