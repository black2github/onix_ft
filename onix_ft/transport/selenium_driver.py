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

from .base import BaseTransport
from .. import config

logger = logging.getLogger("onix_ft.transport")


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
        """Запустить браузер и открыть страницу чата Onix."""
        options_cls = webdriver.EdgeOptions if config.USE_EDGE else webdriver.ChromeOptions
        opts = options_cls()
        opts.add_argument("--disable-extensions")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        if config.BROWSER_PROFILE_DIR:
            opts.add_argument(f"--user-data-dir={config.BROWSER_PROFILE_DIR}")

        if getattr(config, 'USE_WEBDRIVER_MANAGER', False):
            # Автоматическое скачивание подходящего драйвера через webdriver-manager.
            # Удобно на Mac/Linux где версия chromedriver может не совпадать с Chrome.
            # Требует: pip install webdriver-manager
            # Требует доступ в интернет при первом запуске (драйвер кешируется).
            try:
                if config.USE_EDGE:
                    from webdriver_manager.microsoft import EdgeChromiumDriverManager
                    svc = EdgeService(EdgeChromiumDriverManager().install())
                    self._driver = webdriver.Edge(service=svc, options=opts)
                else:
                    from webdriver_manager.chrome import ChromeDriverManager
                    svc = ChromeService(ChromeDriverManager().install())
                    self._driver = webdriver.Chrome(service=svc, options=opts)
                logger.info("Драйвер установлен через webdriver-manager.")
            except ImportError:
                raise ImportError(
                    "USE_WEBDRIVER_MANAGER=True, но webdriver-manager не установлен. "
                    "Выполните: pip install webdriver-manager"
                )
        else:
            drv_path = config.CHROMEDRIVER_PATH or None
            if config.USE_EDGE:
                svc = EdgeService(executable_path=drv_path)
                self._driver = webdriver.Edge(service=svc, options=opts)
            else:
                svc = ChromeService(executable_path=drv_path)
                self._driver = webdriver.Chrome(service=svc, options=opts)

        self._driver.get(config.ONIX_CHAT_URL)
        logger.info("Браузер открыт: %s", config.ONIX_CHAT_URL)

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

        Поле содержит placeholder «Введите сообщение», реализованный через CSS —
        он не является реальным текстом в DOM. Перед вставкой явно очищаем поле
        через Ctrl+A + execCommand('delete'), чтобы удалить любой реальный текст,
        оставшийся с прошлой итерации.

        Стратегия вставки:
          1. execCommand('insertText') — slate.js воспринимает как реальный ввод
             пользователя и активирует кнопку «Отправить».
          2. Если не сработало — буфер обмена (pyperclip + Ctrl+V).
        """
        input_el = self._find(SEL_INPUT_BOX)
        input_el.click()
        time.sleep(0.2)

        # Очищаем поле перед вставкой через JS — без send_keys(Ctrl+A).
        # Причина: send_keys отправляет события клавиатуры в активный элемент
        # браузера. Если пользователь в этот момент работает в другом чате,
        # фокус может сместиться туда и Ctrl+A выделит текст в чужом чате.
        # JS-вызов focus() + selectAll работает точечно с конкретным элементом
        # независимо от того, где находится фокус пользователя.
        self._driver.execute_script(
            "arguments[0].focus();"
            "document.execCommand('selectAll', false, null);"
            "document.execCommand('delete', false, null);",
            input_el
        )
        time.sleep(0.1)

        # Вставляем текст через execCommand('insertText').
        # Slate.js обрабатывает это как реальный пользовательский ввод
        # и активирует кнопку «Отправить».
        inserted = self._driver.execute_script(
            "return document.execCommand('insertText', false, arguments[0]);",
            text
        )

        if not inserted:
            logger.debug("execCommand('insertText') не сработал, пробуем clipboard.")
            self._send_via_clipboard(input_el, text)

        # Ждём пока slate.js активирует кнопку «Отправить».
        # Переход из disabled → enabled происходит асинхронно.
        try:
            send_btn = WebDriverWait(self._driver, config.SEND_BTN_TIMEOUT).until(
                EC.element_to_be_clickable(SEL_SEND_BUTTON)
            )
        except TimeoutException:
            logger.warning(
                "Кнопка «Отправить» не активировалась после JS-вставки. "
                "Пробуем clipboard."
            )
            self._send_via_clipboard(input_el, text)
            send_btn = WebDriverWait(self._driver, config.SEND_BTN_TIMEOUT).until(
                EC.element_to_be_clickable(SEL_SEND_BUTTON)
            )

        send_btn.click()
        time.sleep(config.SEND_DELAY)
        logger.debug("Отправлено %d символов.", len(text))

    def _send_via_clipboard(self, input_el, text: str) -> None:
        """
        Запасной метод вставки через буфер обмена (Ctrl+V).
        Требует библиотеку pyperclip: pip install pyperclip
        """
        try:
            import pyperclip
        except ImportError:
            raise RuntimeError(
                "Не удалось вставить текст через JS, а pyperclip не установлен.\n"
                "Установите: pip install pyperclip"
            )
        input_el.click()
        time.sleep(0.1)
        input_el.send_keys(Keys.CONTROL, 'a')
        time.sleep(0.1)
        pyperclip.copy(text)
        input_el.send_keys(Keys.CONTROL, 'v')
        time.sleep(0.3)

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