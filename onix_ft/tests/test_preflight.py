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


# ── угон стартовой вкладки корпоративной политикой ───────────────────────────

HIJACK_LOG = """\
[1786439807.487][INFO]: Populating Preferences file: {
   "default_search_provider": {
      "url": "https://sfera/SitePages/Search.aspx?q={searchTerms}"
   }
}
[1786439808.717][DEBUG]: DevTools HTTP Response: [ {
   "id": "F90F24EAD83D0BC6FD95FA3BED48C874",
   "type": "page",
   "url": "https://sfera/"
} ]
[1786439808.739][DEBUG]: DevTools WebSocket Command: Target.setAutoAttach (id=3)
[1786439808.739][DEBUG]: DevTools WebSocket Response:  (id=3) browser \
{"code":-32001,"message":"Session with given id not found."}
"""


def test_startup_page_hijack_detects_and_names_page(tmp_path):
    log = tmp_path / "driver.log"
    log.write_text(HIJACK_LOG, encoding="utf-8")
    message = preflight.startup_page_hijack(log)
    assert message is not None
    assert "OPEN_IN_APP_WINDOW" in message              # назван рычаг, которым лечится
    # Названа именно перехватившая вкладка, а не поисковик из дампа Preferences
    assert "открыл https://sfera/ —" in message
    assert "searchTerms" not in message


def test_startup_page_hijack_silent_on_other_logs(tmp_path):
    log = tmp_path / "driver.log"
    log.write_text("[INFO]: Starting ChromeDriver\n[INFO]: RESPONSE InitSession", encoding="utf-8")
    assert preflight.startup_page_hijack(log) is None


def test_startup_page_hijack_survives_missing_log(tmp_path):
    assert preflight.startup_page_hijack(None) is None
    assert preflight.startup_page_hijack(tmp_path / "нет-такого.log") is None


def test_session_failure_hint_replaces_checklist_when_cause_known(tmp_path):
    """Причина установлена — общий чек-лист не показываем, чтобы не путать."""
    log = tmp_path / "driver.log"
    log.write_text(HIJACK_LOG, encoding="utf-8")
    hint = preflight.session_failure_hint(Path(r"C:\onix\profile"), None, log)
    assert "OPEN_IN_APP_WINDOW" in hint
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
