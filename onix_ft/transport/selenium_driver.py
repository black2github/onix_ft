"""
Selenium-транспорт для Onix.

Требования:
    pip install selenium
    chromedriver.exe — путь указать в config.py (CHROMEDRIVER_PATH).

Все пользовательские настройки вынесены в onix_ft/config.py.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Iterator, Optional

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service  import Service as ChromeService
    from selenium.webdriver.edge.service    import Service as EdgeService
    from selenium.webdriver.common.by       import By
    from selenium.webdriver.common.keys     import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.support.ui      import WebDriverWait
    from selenium.webdriver.support         import expected_conditions as EC
    from selenium.common.exceptions         import (
        TimeoutException, NoSuchElementException, StaleElementReferenceException
    )
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

from . import preflight
from .base import BaseTransport
from .. import config

logger = logging.getLogger("onix_ft.transport")


def browser_arguments(
    profile_dir: Optional[Path] = None,
    app_url: Optional[str] = None,
) -> list:
    """
    Ключи командной строки браузера.

    `app_url` (ключ `--app`) задаёт стартовый адрес окна и тем самым отменяет
    стартовые страницы профиля и доменной политики: корпоративный портал
    больше не может забрать вкладку, к которой подключается драйвер.
    Позиционный URL для этого не годится — chromedriver его отбрасывает
    (проверено на chromedriver 149/151).
    """
    args = ["--disable-extensions", "--no-sandbox", "--disable-dev-shm-usage"]
    if profile_dir:
        args.append(f"--user-data-dir={profile_dir}")
    if app_url:
        args.append(f"--app={app_url}")
    return args


# ==============================================================================
#  Селекторы Onix (не требуют ручного редактирования)
# ==============================================================================

# Поле ввода — div[contenteditable="true"] на базе slate.js
SEL_INPUT_BOX   = (By.CSS_SELECTOR, ".slate-message-input")

# Кнопка «Отправить» — активна только когда поле непустое
SEL_SEND_BUTTON = (By.CSS_SELECTOR,
    ".message-input__actions .icon-button--bg-primary")

# Строки сообщений в ленте чата
SEL_MESSAGE_ITEM = (By.CSS_SELECTOR, ".chat-message-row")

# --- Селекторы для очистки истории чата -------------------------------------

# Элемент чата в списке чатов (ищем по имени чата из config.ONIX_CHAT_NAME).
# В DOM Onix это div.chat-list-entry, содержащий span с именем чата.
SEL_CHAT_BUTTON_TMPL = (
    "//div[contains(@class,'chat-list-entry')]"
    "[.//span[contains(@class,'chat-list-entry__name') and contains(text(),'{name}')]]"
)

# Пункт «Очистить историю чата» в контекстном меню
SEL_CLEAR_HISTORY_ITEM = (By.XPATH,
    "//nav[contains(@class,'chat-context-menu')]"
    "//div[contains(@class,'react-contextmenu-item')]"
    "[.//span[text()='Очистить историю чата']]"
)

# Модальное окно подтверждения
SEL_MODAL = (By.CSS_SELECTOR, ".ReactModal__Content")

# Кнопка «Очистить» в модальном окне
SEL_MODAL_CONFIRM = (By.CSS_SELECTOR,
    ".ReactModal__Content button.button--contained--negative")

# ==============================================================================


class OnixSeleniumTransport(BaseTransport):

    def __init__(self):
        if not SELENIUM_AVAILABLE:
            raise ImportError("Selenium не установлен. pip install selenium")
        self._driver: Optional[webdriver.Chrome] = None
        # Множество внутренних element-ID элементов .chat-message-row,
        # которые уже были обработаны. Новыми считаются только те,
        # чей ID ещё не в этом множестве.
        self._seen_ids: set[str] = set()

    # -- Жизненный цикл -------------------------------------------------------

    def open(self):
        """
        Запустить браузер и открыть страницу чата Onix.

        Перед запуском выполняются предполётные проверки (`preflight`):
        доступность каталога профиля, его занятость другим окном браузера и
        совпадение мажорных версий браузера и драйвера. Без них типовая
        ошибка конфигурации выглядела как молчаливое зависание на 120 секунд
        с `ReadTimeoutError` в конце.
        """
        options_cls = webdriver.EdgeOptions if config.USE_EDGE else webdriver.ChromeOptions
        opts = options_cls()

        image = preflight.EDGE_IMAGE if config.USE_EDGE else preflight.CHROME_IMAGE
        profile_dir = preflight.expand_path(config.BROWSER_PROFILE_DIR)
        if profile_dir:
            preflight.ensure_profile_dir(profile_dir)
            preflight.check_profile_free(profile_dir, image)
            logger.info("Профиль браузера: %s", profile_dir)
        else:
            logger.info("Профиль браузера не задан — потребуется ручной логин.")

        app_url = config.ONIX_CHAT_URL if getattr(config, "OPEN_IN_APP_WINDOW", False) else None
        if app_url:
            logger.info("Окно приложения: браузер стартует сразу на %s", app_url)
        for argument in browser_arguments(profile_dir, app_url):
            opts.add_argument(argument)

        drv_path = self._resolve_driver_path()
        self._check_versions(drv_path)

        svc_kwargs = {"executable_path": drv_path}
        driver_log = preflight.expand_path(getattr(config, "DRIVER_LOG_PATH", ""))
        if driver_log:
            driver_log.parent.mkdir(parents=True, exist_ok=True)
            svc_kwargs["service_args"] = ["--verbose", f"--log-path={driver_log}"]
            logger.info("Журнал драйвера: %s", driver_log)

        try:
            if config.USE_EDGE:
                svc = EdgeService(**svc_kwargs)
                self._driver = webdriver.Edge(service=svc, options=opts)
            else:
                svc = ChromeService(**svc_kwargs)
                self._driver = webdriver.Chrome(service=svc, options=opts)
        except Exception as exc:
            raise RuntimeError(
                preflight.session_failure_hint(profile_dir, drv_path, driver_log)
            ) from exc

        self._driver.get(config.ONIX_CHAT_URL)
        logger.info("Браузер открыт: %s", config.ONIX_CHAT_URL)

    @staticmethod
    def _resolve_driver_path() -> Optional[str]:
        """
        Определить путь к драйверу и сказать в журнал, откуда он взят.

        Порядок (первый сработавший источник побеждает):
          1. `CHROMEDRIVER_PATH` — обязательный путь для закрытого контура;
          2. `USE_WEBDRIVER_MANAGER` — автозагрузка (нужен интернет);
          3. ничего не задано — драйвер ищет Selenium Manager (нужен интернет).
        """
        raw = getattr(config, "CHROMEDRIVER_PATH", "") or ""
        if raw:
            path = preflight.expand_path(raw)
            if not path or not path.exists():
                raise preflight.PreflightError(
                    f"CHROMEDRIVER_PATH указывает на несуществующий файл: {raw}\n"
                    "Проверьте путь в onix_ft/config.py — в закрытом контуре "
                    "драйвер подкладывается вручную и путь обязателен."
                )
            logger.info("Драйвер из CHROMEDRIVER_PATH: %s", path)
            return str(path)

        if getattr(config, "USE_WEBDRIVER_MANAGER", False):
            # Автозагрузка — не обязательный источник: её отказ (нет пакета,
            # нет интернета) не должен ронять запуск, дальше пробуем
            # Selenium Manager. Жёсткая ошибка здесь стоила бы работающего
            # внешнего контура ради подсказки для закрытого.
            try:
                if config.USE_EDGE:
                    from webdriver_manager.microsoft import EdgeChromiumDriverManager as Manager
                else:
                    from webdriver_manager.chrome import ChromeDriverManager as Manager

                path = Manager().install()
                logger.info("Драйвер получен через webdriver-manager: %s", path)
                return path
            except ImportError:
                logger.warning(
                    "USE_WEBDRIVER_MANAGER=True, но пакет webdriver-manager не установлен. "
                    "В закрытом контуре автозагрузка невозможна — укажите CHROMEDRIVER_PATH."
                )
            except Exception as exc:
                logger.warning(
                    "Автозагрузка драйвера не удалась (%s). В закрытом контуре "
                    "укажите CHROMEDRIVER_PATH в config.py.", exc
                )

        logger.info(
            "CHROMEDRIVER_PATH не задан — драйвер ищет Selenium Manager (нужен интернет). "
            "В закрытом контуре укажите путь к драйверу в config.py."
        )
        return None

    @staticmethod
    def _check_versions(drv_path: Optional[str]) -> None:
        """Сверить мажоры браузера и драйвера; расхождение — отказ до запуска."""
        if not drv_path:
            return  # драйвер подберёт Selenium Manager — версия согласована им
        browser_version = preflight.detect_browser_version(config.USE_EDGE)
        driver_version  = preflight.detect_driver_version(drv_path)
        logger.info(
            "Версии: браузер %s, драйвер %s",
            browser_version or "не определена",
            driver_version or "не определена",
        )
        conflict = preflight.version_conflict(browser_version, driver_version, config.USE_EDGE)
        if conflict:
            raise preflight.PreflightError(conflict)

    def wait_ready(self, timeout: float = None):
        """
        Ждать появления поля ввода — признак того что страница загружена.
        Если профиль не сохранён — даёт время залогиниться вручную.
        После загрузки помечаем все уже существующие сообщения как виденные,
        чтобы не обрабатывать историю чата.

        Важно: Onix выполняет lazy-loading истории чата — сообщения
        догружаются в DOM асинхронно после появления поля ввода. Поэтому
        после обнаружения поля ввода делаем паузу и повторно обновляем
        seen_ids, чтобы захватить всю подгрузившуюся историю.
        """
        timeout = timeout or config.PAGE_READY_TIMEOUT
        logger.info("Ожидание готовности страницы (до %.0f сек)...", timeout)
        try:
            WebDriverWait(self._driver, timeout).until(
                EC.presence_of_element_located(SEL_INPUT_BOX)
            )
            # Первый снимок — захватываем то, что уже есть в DOM
            self._refresh_seen_ids()

            # Пауза для завершения lazy-loading истории чата.
            # Onix подгружает историю асинхронно после рендера страницы —
            # без паузы часть старых сообщений появится в DOM уже после
            # того как seen_ids был заполнен, и будет ошибочно принята
            # за новые входящие сообщения.
            logger.info(
                "Ожидание догрузки истории чата (%.0f сек)...",
                config.HISTORY_SETTLE_TIME
            )
            time.sleep(config.HISTORY_SETTLE_TIME)

            # Второй снимок — захватываем всё что догрузилось за паузу
            self._refresh_seen_ids()

        except TimeoutException:
            raise RuntimeError(
                "Поле ввода не появилось за отведённое время. "
                "Проверьте логин и значение SEL_INPUT_BOX."
            )

    def _refresh_seen_ids(self):
        """
        Обновить множество виденных элементов — пометить всё что сейчас
        есть в ленте как уже обработанное.
        Вызывается после очистки истории или при необходимости сбросить курсор.
        """
        items = self._driver.find_elements(*SEL_MESSAGE_ITEM)
        for el in items:
            self._seen_ids.add(self._element_id(el))
        logger.info(
            "Помечено как виденных: %d элементов в ленте.", len(items)
        )

    def close(self):
        """Закрыть браузер и освободить ресурсы."""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
            logger.info("Браузер закрыт.")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # -- Очистка истории чата -------------------------------------------------

    def clear_chat_history(self, chat_name: str = None) -> bool:
        """
        Очистить историю чата через контекстное меню Onix.

        Последовательность действий:
          1. Правый клик по кнопке чата в списке → открывается контекстное меню.
          2. Клик по пункту «Очистить историю чата».
          3. В модальном окне подтверждения — клик по кнопке «Очистить».
          4. Обновить _seen_ids (теперь лента пуста).

        Параметры:
            chat_name: имя чата из списка. Если не указано — берётся из
                       config.ONIX_CHAT_NAME.

        Возвращает True при успехе, False если что-то пошло не так.
        """
        name = chat_name or config.ONIX_CHAT_NAME
        logger.info("Очистка истории чата: «%s»...", name)

        try:
            # Шаг 1: найти кнопку чата в списке и открыть контекстное меню
            xpath = SEL_CHAT_BUTTON_TMPL.format(name=name)
            chat_btn = WebDriverWait(self._driver, config.ELEMENT_WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
            ActionChains(self._driver).context_click(chat_btn).perform()
            logger.debug("Контекстное меню открыто.")

            # Шаг 2: клик по пункту «Очистить историю чата»
            clear_item = WebDriverWait(self._driver, config.ELEMENT_WAIT_TIMEOUT).until(
                EC.element_to_be_clickable(SEL_CLEAR_HISTORY_ITEM)
            )
            clear_item.click()
            logger.debug("Пункт «Очистить историю чата» нажат.")

            # Шаг 3: ждём модальное окно и нажимаем «Очистить»
            WebDriverWait(self._driver, config.ELEMENT_WAIT_TIMEOUT).until(
                EC.presence_of_element_located(SEL_MODAL)
            )
            confirm_btn = WebDriverWait(self._driver, config.ELEMENT_WAIT_TIMEOUT).until(
                EC.element_to_be_clickable(SEL_MODAL_CONFIRM)
            )
            # JS-клик на случай если кнопка перекрыта другим элементом
            self._driver.execute_script("arguments[0].click();", confirm_btn)
            logger.debug("Кнопка «Очистить» нажата.")

            # Шаг 4: ждём закрытия модального окна и обновляем seen_ids
            WebDriverWait(self._driver, config.ELEMENT_WAIT_TIMEOUT).until(
                EC.invisibility_of_element_located(SEL_MODAL)
            )
            time.sleep(0.5)  # небольшая пауза для завершения анимации
            self._seen_ids.clear()
            self._refresh_seen_ids()

            logger.info("История чата «%s» очищена.", name)
            return True

        except TimeoutException as e:
            logger.error(
                "Таймаут при очистке истории чата «%s»: %s. "
                "Проверьте что чат виден в списке и имя задано точно.",
                name, e
            )
            return False
        except Exception as e:
            logger.error("Ошибка при очистке истории чата: %s", e)
            return False

    # -- Отправка --------------------------------------------------------------

    def send(self, text: str) -> None:
        """
        Вставить текст в поле ввода slate.js и отправить.

        Четыре метода вставки по убыванию приоритета:
          1. execCommand('insertText') — основной, мгновенный.
          2. pyperclip + Ctrl+V — если метод 1 не активировал кнопку.
          3. send_keys посимвольно — для VDI где буфер обмена заблокирован.
          4. Enter напрямую в поле — обход кнопки «Отправить» через клавишу.
             На слабых машинах (8 ГБ RAM) slate.js может не успеть активировать
             кнопку, но Enter всегда обрабатывается независимо от состояния кнопки.
        """
        input_el = self._find(SEL_INPUT_BOX)
        input_el.click()
        time.sleep(0.2)

        # Очищаем поле через JS — без send_keys чтобы не перехватить фокус.
        self._driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('selectAll', false, null);"
            "document.execCommand('delete', false, null);",
            input_el
        )
        time.sleep(0.1)

        # Метод 1: execCommand('insertText')
        inserted = self._driver.execute_script(
            "return document.execCommand('insertText', false, arguments[0]);",
            text
        )
        if inserted and self._wait_for_send_btn(raise_on_fail=False):
            self._click_send_btn()
            return

        # Метод 2: буфер обмена (pyperclip + Ctrl+V)
        logger.warning("Метод 1 (execCommand) не сработал — пробуем clipboard.")
        if self._send_via_clipboard(input_el, text):
            if self._wait_for_send_btn(raise_on_fail=False):
                self._click_send_btn()
                return

        # Метод 3: send_keys посимвольно
        logger.warning("Метод 2 (clipboard) не сработал — пробуем send_keys.")
        self._send_via_keys(input_el, text)
        if self._wait_for_send_btn(raise_on_fail=False):
            self._click_send_btn()
            return

        # Метод 4: отправка через Enter напрямую в поле ввода.
        # Обходит кнопку «Отправить» полностью — на слабых машинах slate.js
        # может не успеть активировать кнопку, но Enter всегда работает
        # если текст присутствует в поле. Пробуем вставить заново и нажать Enter.
        logger.warning(
            "Метод 3 (send_keys) не активировал кнопку — "
            "пробуем вставку + Enter."
        )
        self._driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('selectAll', false, null);"
            "document.execCommand('delete', false, null);",
            input_el
        )
        time.sleep(0.2)
        self._driver.execute_script(
            "document.execCommand('insertText', false, arguments[0]);",
            text
        )
        time.sleep(0.5)  # дать slate.js время обработать текст
        input_el.send_keys(Keys.RETURN)
        time.sleep(config.SEND_DELAY)
        logger.debug("Отправлено через Enter, %d символов.", len(text))

    def _wait_for_send_btn(self, raise_on_fail: bool = True) -> bool:
        """
        Ждать активации кнопки «Отправить».
        Возвращает True если кнопка стала активна, False если таймаут.
        """
        try:
            WebDriverWait(
                self._driver, config.SEND_BTN_TIMEOUT, poll_frequency=0.1
            ).until(EC.element_to_be_clickable(SEL_SEND_BUTTON))
            return True
        except TimeoutException:
            if raise_on_fail:
                raise
            return False

    def _click_send_btn(self) -> None:
        """Нажать кнопку «Отправить» и выдержать паузу."""
        btn = self._driver.find_element(*SEL_SEND_BUTTON)
        btn.click()
        time.sleep(config.SEND_DELAY)
        logger.debug("Отправлено.")

    def _send_via_clipboard(self, input_el, text: str) -> bool:
        """
        Вставка через буфер обмена (Ctrl+V).
        Возвращает True если удалось, False если pyperclip не установлен.
        """
        try:
            import pyperclip
        except ImportError:
            logger.warning("pyperclip не установлен. pip install pyperclip")
            return False
        try:
            input_el.click()
            time.sleep(0.1)
            input_el.send_keys(Keys.CONTROL, 'a')
            time.sleep(0.1)
            pyperclip.copy(text)
            input_el.send_keys(Keys.CONTROL, 'v')
            time.sleep(0.3)
            return True
        except Exception as e:
            logger.warning("Ошибка вставки через clipboard: %s", e)
            return False

    def _send_via_keys(self, input_el, text: str) -> None:
        """
        Вставка через send_keys посимвольно.
        Медленно, но работает на VDI где execCommand и clipboard заблокированы.
        """
        input_el.click()
        time.sleep(0.1)
        input_el.send_keys(Keys.CONTROL, 'a')
        time.sleep(0.1)
        input_el.send_keys(Keys.DELETE)
        time.sleep(0.1)
        CHUNK = 200
        for i in range(0, len(text), CHUNK):
            input_el.send_keys(text[i:i + CHUNK])
            time.sleep(0.05)
        logger.debug("send_keys: вставлено %d символов.", len(text))

    # -- Чтение новых сообщений -----------------------------------------------

    def poll_new_messages(self) -> Iterator[str]:
        """
        Вернуть тексты новых сообщений — только те элементы .chat-message-row,
        чей внутренний element-ID ещё не встречался.

        Стратегия извлечения текста (по убыванию специфичности):
          1. .chat-message__bubble  — основной контейнер текста сообщения
          2. .chat-message__text    — альтернативный контейнер
          3. el.text                — весь текст строки как запасной вариант
        Пустые строки (служебные элементы, разделители дат) отфильтровываются.
        """
        try:
            items = self._driver.find_elements(*SEL_MESSAGE_ITEM)
        except Exception:
            return

        new_items = [el for el in items
                     if self._element_id(el) not in self._seen_ids]

        logger.debug(
            "Всего элементов в ленте: %d, новых: %d",
            len(items), len(new_items)
        )

        for el in new_items:
            # Помечаем как виденный вне зависимости от того, извлечём текст или нет.
            self._seen_ids.add(self._element_id(el))
            try:
                text = self._extract_text(el)
                if text:
                    logger.debug("Новое сообщение: %r", text[:80])
                    yield text
                else:
                    logger.debug("Пустой элемент (служебный?) — пропускаем.")
            except StaleElementReferenceException:
                # Элемент исчез из DOM пока мы его читали — пропускаем.
                continue

    # -- Вспомогательные методы -----------------------------------------------

    def _extract_text(self, row_el) -> str:
        """
        Извлечь текст сообщения из строки .chat-message-row.
        Пробуем несколько селекторов, так как структура DOM может отличаться
        для входящих и исходящих сообщений.

        Onix добавляет временну́ю метку (HH:MM) в конец текстового содержимого
        пузыря как отдельную строку — отфильтровываем её через _strip_timestamp().
        """
        for selector in (
            ".chat-message__bubble",
            ".chat-message__text",
        ):
            try:
                child = row_el.find_element(By.CSS_SELECTOR, selector)
                text = self._strip_timestamp(child.text.strip())
                if text:
                    return text
            except NoSuchElementException:
                continue

        # Запасной вариант — весь текст строки
        return self._strip_timestamp(row_el.text.strip())

    # Паттерн временно́й метки Onix: H:MM или HH:MM
    _TIMESTAMP_RE = re.compile(r'^\d{1,2}:\d{2}$')

    def _strip_timestamp(self, text: str) -> str:
        """
        Убрать временну́ю метку из текста сообщения.
        Onix добавляет время отправки (HH:MM) последней строкой в пузырь.
        Фильтруем строки, целиком совпадающие с паттерном H:MM / HH:MM.
        """
        lines = text.splitlines()
        filtered = [l for l in lines if not self._TIMESTAMP_RE.match(l.strip())]
        return '\n'.join(filtered).strip()

    def _element_id(self, el) -> str:
        """Получить внутренний WebDriver ID элемента для отслеживания уникальности."""
        return el.id

    def _find(self, locator: tuple, timeout: float = None):
        """Найти элемент с ожиданием появления."""
        t = timeout or config.ELEMENT_WAIT_TIMEOUT
        return WebDriverWait(self._driver, t).until(
            EC.presence_of_element_located(locator)
        )

    def snapshot_dom_for_selectors(self, output_path: str = "onix_dom_snapshot.html"):
        """
        Сохранить полный HTML страницы в файл для анализа селекторов.
        Вызывать один раз вручную после открытия нужного чата.
        """
        html = self._driver.page_source
        Path(output_path).write_text(html, encoding="utf-8")
        logger.info("DOM сохранён: %s (%d байт)", output_path, len(html))
        return output_path