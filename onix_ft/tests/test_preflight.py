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

from onix_ft.transport import preflight
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


def test_session_failure_hint_points_to_existing_log():
    hint = preflight.session_failure_hint(
        Path(r"C:\onix\profile"), r"C:\onix\chromedriver.exe", Path(r"C:\onix\cd.log")
    )
    assert r"C:\onix\cd.log" in hint


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
