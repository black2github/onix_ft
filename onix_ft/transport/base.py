"""
Абстрактный транспорт — интерфейс, который должен реализовать каждый драйвер.

Сейчас есть два драйвера:
  - OnixSeleniumTransport  (transport/selenium_driver.py) — реальная работа
  - StubTransport          (этот файл)                   — для юнит-тестов без браузера
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Iterator


class BaseTransport(ABC):

    @abstractmethod
    def send(self, text: str) -> None:
        """Отправить текстовое сообщение в чат Onix."""
        ...

    @abstractmethod
    def poll_new_messages(self) -> Iterator[str]:
        """
        Генератор: вернуть текст новых сообщений, появившихся с момента
        последнего вызова. Порядок — от старых к новым.
        Должен возвращать только ЕЩЁ НЕ ОБРАБОТАННЫЕ сообщения.
        """
        ...

    def wait_for_message(
        self,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> Iterator[str]:
        """
        Блокирующий опрос: ждать хотя бы одного нового сообщения
        до timeout секунд, возвращая сообщения по мере появления.
        Бросает TimeoutError, если ничего не пришло за timeout секунд.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = list(self.poll_new_messages())
            if messages:
                yield from messages
                return
            time.sleep(poll_interval)
        raise TimeoutError(f"Нет новых сообщений за {timeout:.0f} секунд")

    def close(self) -> None:
        """Освободить ресурсы (закрыть браузер и т.п.)."""
        pass


# ── стаб для тестов ──────────────────────────────────────────────────────────

class StubTransport(BaseTransport):
    """
    Простейший стаб: обе стороны (отправитель и получатель) живут в одном
    процессе и общаются через общую очередь в памяти.

    Использование в тестах:
        queue = []
        sender   = StubTransport(inbox=queue, outbox=other_queue)
        receiver = StubTransport(inbox=other_queue, outbox=queue)
    """

    def __init__(self, inbox: list[str], outbox: list[str]):
        self._inbox   = inbox   # сюда другая сторона кладёт сообщения
        self._outbox  = outbox  # сюда мы кладём свои сообщения
        self._cursor  = 0       # сколько сообщений из inbox уже обработали

    def send(self, text: str) -> None:
        self._outbox.append(text)

    def poll_new_messages(self) -> Iterator[str]:
        new = self._inbox[self._cursor:]
        self._cursor += len(new)
        yield from new
