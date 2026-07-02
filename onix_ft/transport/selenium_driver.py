"""
Selenium-транспорт для Onix.

Этот файл содержит всю браузерную автоматизацию.
Селекторы вынесены в константы вверху файла — вы заполните их после
того, как разберёмся с DOM Onix.

Требования:
    pip install selenium
    chromedriver.exe (или msedgedriver.exe) рядом со скриптом
    или путь в CHROMEDRIVER_PATH.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterator, Optional

# ── импорт Selenium ──────────────────────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.edge.service  import Service as EdgeService
    from selenium.webdriver.common.by     import By
    from selenium.webdriver.common.keys   import Keys
    from selenium.webdriver.support.ui    import WebDriverWait
    from selenium.webdriver.support       import expected_conditions as EC
    from selenium.common.exceptions       import (
        TimeoutException, NoSuchElementException, StaleElementReferenceException
    )
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

from .base import BaseTransport

logger = logging.getLogger("onix_ft.transport")


# ══════════════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ — заполните перед первым запуском
# ══════════════════════════════════════════════════════════════════════════════

# Путь к chromedriver.exe / msedgedriver.exe
# Если добавлен в PATH — можно оставить пустым ("").
CHROMEDRIVER_PATH: str = r"C:\tools\chromedriver\chromedriver.exe"

# Использовать Edge вместо Chrome?
USE_EDGE: bool = False

# URL чата Onix — страница с нужным диалогом (откройте вручную, скопируйте URL)
ONIX_CHAT_URL: str = "https://onix.your-company.ru/chat/ROOM_ID"

# Путь к профилю браузера — чтобы не логиниться каждый раз.
# Chrome:  %LOCALAPPDATA%\Google\Chrome\User Data
# Edge:    %LOCALAPPDATA%\Microsoft\Edge\User Data
# Оставьте "" — браузер откроется без профиля (потребуется ручной логин).
BROWSER_PROFILE_DIR: str = ""

# ── CSS/XPath-селекторы (TODO: заполнить по результатам анализа DOM Onix) ───

# Поле ввода сообщения (textarea или div[contenteditable])
# TODO: замените на реальный селектор
SEL_INPUT_BOX = (By.CSS_SELECTOR, "TODO_input_selector")

# Кнопка «Отправить» (если нужна — иногда достаточно Enter в поле ввода)
# TODO: замените или оставьте None, если Enter работает
SEL_SEND_BUTTON: Optional[tuple] = None  # например: (By.CSS_SELECTOR, "button.send-btn")

# Контейнер со списком сообщений
# TODO: замените на реальный селектор
SEL_MESSAGES_CONTAINER = (By.CSS_SELECTOR, "TODO_messages_container")

# Отдельное сообщение чата (дочерние элементы контейнера)
# TODO: замените на реальный селектор
SEL_MESSAGE_ITEM = (By.CSS_SELECTOR, "TODO_message_item")

# Текстовое содержимое сообщения внутри элемента сообщения
# Если текст — innerText самого элемента, оставьте None.
# Если текст в дочернем элементе — укажите его CSS-селектор (относительный).
SEL_MESSAGE_TEXT_CHILD: Optional[str] = None  # например: ".message-text"

# Таймаут ожидания появления элементов на странице (секунды)
ELEMENT_WAIT_TIMEOUT: float = 15.0

# Задержка после отправки сообщения перед следующим действием (секунды)
SEND_DELAY: float = 1.0

# ══════════════════════════════════════════════════════════════════════════════


class OnixSeleniumTransport(BaseTransport):
    """
    Транспорт на основе Selenium WebDriver.

    Жизненный цикл:
        t = OnixSeleniumTransport()
        t.open()          # открыть браузер и перейти на страницу чата
        # ... при необходимости: ручной логин в браузере, затем t.wait_ready() ...
        t.send("text")
        for msg in t.poll_new_messages(): ...
        t.close()

    Или через контекстный менеджер:
        with OnixSeleniumTransport() as t:
            t.send("hello")
    """

    def __init__(self):
        if not SELENIUM_AVAILABLE:
            raise ImportError("Selenium не установлен. pip install selenium")
        self._driver: Optional[webdriver.Chrome] = None
        self._last_msg_count: int = 0   # сколько сообщений видели в последний раз

    # ── жизненный цикл ───────────────────────────────────────────────────────

    def open(self):
        """Запустить браузер и открыть страницу Onix."""
        options_cls = webdriver.EdgeOptions if USE_EDGE else webdriver.ChromeOptions

        opts = options_cls()
        opts.add_argument("--disable-extensions")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

        if BROWSER_PROFILE_DIR:
            opts.add_argument(f"--user-data-dir={BROWSER_PROFILE_DIR}")

        if USE_EDGE:
            svc = EdgeService(executable_path=CHROMEDRIVER_PATH or None)
            self._driver = webdriver.Edge(service=svc, options=opts)
        else:
            svc = ChromeService(executable_path=CHROMEDRIVER_PATH or None)
            self._driver = webdriver.Chrome(service=svc, options=opts)

        self._driver.get(ONIX_CHAT_URL)
        logger.info("Браузер открыт: %s", ONIX_CHAT_URL)

    def wait_ready(self, timeout: float = 120.0):
        """
        Подождать, пока страница загрузится и поле ввода станет кликабельным.
        Даёт время на ручной логин, если профиль не сохранён.
        """
        logger.info("Ожидание готовности страницы (до %.0f сек)...", timeout)
        try:
            WebDriverWait(self._driver, timeout).until(
                EC.presence_of_element_located(SEL_INPUT_BOX)
            )
            logger.info("Страница готова.")
        except TimeoutException:
            raise RuntimeError(
                "Поле ввода не появилось за отведённое время. "
                "Проверьте логин и селектор SEL_INPUT_BOX."
            )

    def close(self):
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

    # ── отправка ─────────────────────────────────────────────────────────────

    def send(self, text: str) -> None:
        """
        Вставить текст в поле ввода и отправить.

        Важно: для длинных строк base64 НЕ используем Keys.send_keys —
        это медленно (посимвольно). Вставляем через JS + clipboard trick.
        """
        input_el = self._find(SEL_INPUT_BOX)

        # Вставка через JS — мгновенно, без посимвольного ввода.
        # Работает для <input> и <textarea>.
        # Для div[contenteditable] — см. ветку ниже.
        tag = input_el.tag_name.lower()
        if tag in ("input", "textarea"):
            self._driver.execute_script(
                "arguments[0].value = arguments[1];", input_el, text
            )
            # Trigger input event so the framework (React/Vue/Angular) замечает изменение
            self._driver.execute_script(
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                input_el
            )
        else:
            # contenteditable div
            self._driver.execute_script(
                "arguments[0].innerText = arguments[1];", input_el, text
            )
            self._driver.execute_script(
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                input_el
            )

        time.sleep(0.3)

        # Отправка: кнопкой или Enter
        if SEL_SEND_BUTTON:
            self._find(SEL_SEND_BUTTON).click()
        else:
            input_el.send_keys(Keys.RETURN)

        time.sleep(SEND_DELAY)
        logger.debug("Отправлено %d символов", len(text))

    # ── чтение новых сообщений ───────────────────────────────────────────────

    def poll_new_messages(self) -> Iterator[str]:
        """
        Вернуть тексты новых сообщений (только те, что появились
        с момента последнего вызова).
        """
        try:
            container = self._find(SEL_MESSAGES_CONTAINER, timeout=3.0)
            items     = container.find_elements(*SEL_MESSAGE_ITEM)
        except Exception:
            return

        new_items = items[self._last_msg_count:]
        self._last_msg_count = len(items)

        for el in new_items:
            try:
                if SEL_MESSAGE_TEXT_CHILD:
                    text = el.find_element(By.CSS_SELECTOR, SEL_MESSAGE_TEXT_CHILD).text
                else:
                    text = el.text
                if text:
                    yield text.strip()
            except (StaleElementReferenceException, NoSuchElementException):
                # Элемент пропал из DOM пока мы его читали — пропускаем
                continue

    # ── вспомогательные методы ───────────────────────────────────────────────

    def _find(self, locator: tuple, timeout: float = ELEMENT_WAIT_TIMEOUT):
        return WebDriverWait(self._driver, timeout).until(
            EC.presence_of_element_located(locator)
        )

    def snapshot_dom_for_selectors(self, output_path: str = "onix_dom_snapshot.html"):
        """
        Сохранить текущий HTML страницы в файл — для анализа и подбора селекторов.
        Вызвать один раз вручную после открытия чата.
        """
        html = self._driver.page_source
        Path(output_path).write_text(html, encoding="utf-8")
        logger.info("DOM сохранён: %s (%d байт)", output_path, len(html))
        return output_path
