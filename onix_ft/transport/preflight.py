"""
Предполётные проверки запуска браузера.

Задача модуля — превратить молчаливое зависание в понятное сообщение.
Типовой отказ выглядит так: chromedriver стартовал и слушает порт, но
браузер не поднялся; selenium ждёт ответа на NEW_SESSION и через 120 секунд
падает с `ReadTimeoutError` — из которого причина никак не следует.

Проверяемые причины (инцидент закрытого контура, 2026-08-11):
  * `--user-data-dir` ведёт в профиль ДРУГОГО пользователя Windows
    (в общем бандле путь был зашит вместе с чужим логином);
  * каталог профиля уже занят запущенным браузером;
  * мажорные версии браузера и драйвера разошлись;
  * профиль лежит на сетевом пути (перенаправленный AppData на VDI).

Модуль намеренно НЕ импортирует selenium: функции чистые и проверяются
тестами без браузера и без драйвера.
"""

from __future__ import annotations

import csv
import getpass
import io
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

# Имя процесса браузера по флагу config.USE_EDGE
CHROME_IMAGE = "chrome.exe"
EDGE_IMAGE   = "msedge.exe"

# Сколько секунд ждать вспомогательные команды диагностики.
# Проверки выполняются один раз при старте передачи, поэтому лимиты щадящие.
PROBE_TIMEOUT = 15


class PreflightError(RuntimeError):
    """Отказ предполётной проверки: причина известна, запуск бессмысленен."""


# ── пути ─────────────────────────────────────────────────────────────────────

def expand_path(raw: Optional[str]) -> Optional[Path]:
    """
    Развернуть путь из конфига: `%LOCALAPPDATA%`, `$HOME`, `~`.

    Пустая строка/None → None (настройка не задана). Развёртывание позволяет
    держать в общем бандле ОДИН путь на всех пользователей — без зашитого
    логина, из-за которого бандл ломался у всех, кроме автора конфига.
    """
    if not raw:
        return None
    expanded = os.path.expandvars(str(raw).strip())
    if not expanded:
        return None
    return Path(os.path.expanduser(expanded))


def current_user() -> str:
    """Логин текущего пользователя Windows (для диагностики чужих путей)."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "")


def foreign_user_dir(path: Path) -> Optional[str]:
    """
    Путь ведёт в профиль другого пользователя? Вернуть его логин, иначе None.

    Ищем шаблон `<диск>:\\Users\\<логин>\\…` и сравниваем `<логин>` с текущим
    пользователем без учёта регистра.
    """
    parts = Path(path).parts
    for i, part in enumerate(parts):
        if part.strip("\\/").lower() in ("users", "documents and settings"):
            if i + 1 < len(parts):
                owner = parts[i + 1]
                me = current_user()
                if me and owner.lower() != me.lower():
                    return owner
            return None
    return None


def is_network_path(path: Path) -> bool:
    """
    Путь сетевой (UNC или подключённый сетевой диск)?

    На VDI перенаправленный профиль — частая причина зависания Chrome при
    старте, поэтому о таком пути предупреждаем заранее.
    """
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if os.name != "nt":
        return False
    drive = os.path.splitdrive(text)[0]
    if not drive:
        return False
    try:
        import ctypes

        DRIVE_REMOTE = 4
        return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == DRIVE_REMOTE
    except Exception:
        return False


def ensure_profile_dir(path: Path) -> None:
    """
    Каталог профиля существует и доступен на запись — иначе `PreflightError`.

    Именно здесь ловится главный отказ: каталог из чужого профиля Windows
    создать нельзя, Chrome не стартует, а без этой проверки пользователь
    видит только таймаут через две минуты.
    """
    path = Path(path)
    owner = foreign_user_dir(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".onixft-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        lines = [
            f"Каталог профиля браузера недоступен: {path}",
            f"Причина: {exc}",
        ]
        if owner:
            lines.append(
                f"Путь ведёт в профиль другого пользователя Windows ({owner}), "
                f"а вы вошли как {current_user()}."
            )
        lines.append(
            "Поправьте BROWSER_PROFILE_DIR в onix_ft/config.py — например "
            r'r"%LOCALAPPDATA%\Google\Chrome\OnixFT" (у каждого пользователя свой) '
            r'или локальный каталог r"C:\onix\profile-onixft".'
        )
        raise PreflightError("\n".join(lines)) from exc

    if owner:
        # Записать смогли, но каталог всё равно чужой — блокировать не за что,
        # предупредить обязаны (правило: не терять данные и не мешать работе).
        _warn(
            f"Каталог профиля лежит в пользователе {owner}, а вы {current_user()} — "
            "проверьте BROWSER_PROFILE_DIR, если браузер поведёт себя странно."
        )

    if is_network_path(path):
        _warn(
            f"Каталог профиля {path} находится на сетевом пути. На VDI "
            "перенаправленный профиль часто подвешивает запуск браузера — "
            r'надёжнее локальный путь, например r"C:\onix\profile-onixft".'
        )


def _warn(message: str) -> None:
    """Предупреждение через logging (импорт локальный — модуль остаётся чистым)."""
    import logging

    logging.getLogger("onix_ft.transport").warning(message)


# ── версии браузера и драйвера ───────────────────────────────────────────────

def parse_major(version: Optional[str]) -> Optional[int]:
    """Мажор версии из строки вида `147.0.7727.117` → 147; мусор → None."""
    if not version:
        return None
    match = re.search(r"(\d+)(?:\.\d+)*", str(version))
    return int(match.group(1)) if match else None


def find_browser_binary(use_edge: bool = False) -> Optional[Path]:
    """Путь к chrome.exe / msedge.exe: реестр App Paths, затем типовые каталоги."""
    exe = EDGE_IMAGE if use_edge else CHROME_IMAGE
    if os.name == "nt":
        try:
            import winreg

            key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(root, key_path) as key:
                        value = winreg.QueryValue(key, None)
                        if value and Path(value).exists():
                            return Path(value)
                except OSError:
                    continue
        except Exception:
            pass

    vendor = "Microsoft/Edge" if use_edge else "Google/Chrome"
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / vendor / "Application" / exe,
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / vendor / "Application" / exe,
        Path(os.environ.get("LOCALAPPDATA", "")) / vendor / "Application" / exe,
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def detect_browser_version(use_edge: bool = False) -> Optional[str]:
    """
    Версия установленного браузера.

    Порядок: ключ реестра BLBeacon (заполняется установщиком), затем — имя
    каталога версии рядом с исполняемым файлом (`…/Application/147.0.7727.117/`).
    """
    if os.name == "nt":
        try:
            import winreg

            key_path = r"Software\Microsoft\Edge\BLBeacon" if use_edge else r"Software\Google\Chrome\BLBeacon"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "version")
                if value:
                    return str(value)
        except Exception:
            pass

    binary = find_browser_binary(use_edge)
    if binary:
        try:
            versions = [
                child.name for child in binary.parent.iterdir()
                if child.is_dir() and re.fullmatch(r"\d+(\.\d+)+", child.name)
            ]
            if versions:
                return sorted(versions, key=lambda v: [int(p) for p in v.split(".")])[-1]
        except OSError:
            pass
    return None


def detect_driver_version(driver_path: Optional[str]) -> Optional[str]:
    """Версия драйвера через `chromedriver.exe --version`; недоступен → None."""
    if not driver_path:
        return None
    try:
        result = subprocess.run(
            [str(driver_path), "--version"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"(\d+(?:\.\d+)+)", result.stdout or "")
    return match.group(1) if match else None


def version_conflict(
    browser_version: Optional[str],
    driver_version: Optional[str],
    use_edge: bool = False,
) -> Optional[str]:
    """
    Текст ошибки при расхождении мажоров, иначе None.

    Если версию хотя бы с одной стороны определить не удалось — конфликт НЕ
    объявляем: запрет по неполным данным хуже, чем пропущенное предупреждение
    (запуск всё равно упрётся в понятную ошибку драйвера).
    """
    browser_major = parse_major(browser_version)
    driver_major  = parse_major(driver_version)
    if browser_major is None or driver_major is None:
        return None
    if browser_major == driver_major:
        return None
    name = "Edge" if use_edge else "Chrome"
    driver_name = "msedgedriver" if use_edge else "chromedriver"
    return (
        f"Версии не совпадают: {name} {browser_version} против "
        f"{driver_name} {driver_version} (мажоры {browser_major} и {driver_major}). "
        f"Драйвер обязан быть той же мажорной версии. Подложите {driver_name} "
        f"версии {browser_major}.x и укажите его в CHROMEDRIVER_PATH."
    )


# ── занятость профиля ────────────────────────────────────────────────────────

def any_browser_running(image: str = CHROME_IMAGE) -> Optional[bool]:
    """Есть ли вообще процессы браузера (быстрый фильтр); неизвестно → None."""
    if os.name != "nt":
        return None
    tasklist = shutil.which("tasklist")
    if not tasklist:
        return None
    try:
        result = subprocess.run(
            [tasklist, "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return image.lower() in (result.stdout or "").lower()


def _profile_from_command_line(command_line: str) -> Optional[str]:
    """Значение `--user-data-dir` из командной строки процесса."""
    match = re.search(r'--user-data-dir=(?:"([^"]*)"|(\S+))', command_line or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


def _same_path(left: str, right: str) -> bool:
    """Сравнение путей без учёта регистра, слешей и хвостового разделителя."""
    def norm(value: str) -> str:
        return os.path.normcase(os.path.normpath(str(value).strip().strip('"'))).rstrip("\\/")

    return norm(left) == norm(right)


def processes_using_profile(profile_dir: Path, image: str = CHROME_IMAGE) -> Optional[List[int]]:
    """
    PID процессов браузера, занявших этот каталог профиля.

    None — проверить не удалось (нет PowerShell, запрет политики, таймаут):
    это НЕ значит «свободно», поэтому вызывающий трактует None как «неизвестно»
    и не блокирует запуск.
    """
    if os.name != "nt":
        return None
    if any_browser_running(image) is False:
        return []

    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return None

    query = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{image}'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Csv -NoTypeInformation"
    )
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout:
        return None

    pids: List[int] = []
    try:
        rows = list(csv.reader(io.StringIO(result.stdout)))
    except Exception:
        return None
    for row in rows:
        if len(row) < 2 or row[0] == "ProcessId":
            continue
        used = _profile_from_command_line(row[1])
        if used and _same_path(used, str(profile_dir)):
            try:
                pids.append(int(row[0]))
            except ValueError:
                continue
    return pids


def check_profile_free(profile_dir: Path, image: str = CHROME_IMAGE) -> None:
    """
    Профиль не занят живым браузером — иначе `PreflightError`.

    Занятый каталог профиля — вторая по частоте причина зависания NEW_SESSION:
    второй экземпляр браузера не может залочить профиль и не отдаёт драйверу
    отладочный порт.
    """
    pids = processes_using_profile(profile_dir, image)
    if pids is None:
        return  # проверить не смогли — молча пропускаем, запуск не блокируем
    if not pids:
        return
    listed = ", ".join(str(pid) for pid in pids)
    raise PreflightError(
        f"Каталог профиля {profile_dir} уже занят процессом {image} (PID {listed}).\n"
        "Закройте этот браузер или укажите в BROWSER_PROFILE_DIR отдельный "
        "каталог — иначе драйвер будет ждать браузер до таймаута."
    )


# ── подсказка при сорванном старте сессии ────────────────────────────────────

def session_failure_hint(
    profile_dir: Optional[Path],
    driver_path: Optional[str],
    driver_log: Optional[Path] = None,
) -> str:
    """Человекочитаемый разбор для исключения, когда сессия так и не поднялась."""
    lines = [
        "Браузер не поднялся: драйвер запущен, но сессия не создана.",
        "Что проверить по порядку:",
        f"  1. Профиль {profile_dir or '(не задан)'} — доступен на запись, "
        "не занят другим окном браузера, лежит на локальном диске.",
        f"  2. Драйвер {driver_path or '(ищет Selenium Manager)'} — той же "
        "мажорной версии, что и браузер, и той же разрядности.",
        "  3. Антивирус или политика домена не блокируют запуск браузера "
        "дочерним процессом.",
    ]
    if driver_log:
        lines.append(f"  4. Журнал драйвера: {driver_log}")
    else:
        lines.append(
            "  4. Включите журнал драйвера — DRIVER_LOG_PATH в onix_ft/config.py — "
            "и повторите запуск: причина будет видна дословно."
        )
    return "\n".join(lines)
