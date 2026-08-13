"""
Передача через канал, который ТЕРЯЕТ сообщения.

Инцидент 2026-08-13 (закрытый контур): по снимку ленты около 15 % сообщений
не доходило до получателя с первого раза. Прежний приёмник выбрасывал
пришедшие «вперёд» блоки и требовал повтор всего окна, а потерю ПОСЛЕДНЕГО
блока окна вообще не замечал — NACK слался только при виде внеочередного
блока, поэтому обе стороны молча ждали таймаутов.

Здесь потери воспроизводятся детерминированно, без браузера и без сети.

Запуск:
    python -m pytest onix_ft/tests/test_lossy_channel.py -v
"""

import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft import config
from onix_ft.core.protocol import Frame, FrameType, file_sha256
from onix_ft.core.receiver import FileReceiver
from onix_ft.core.sender   import FileSender
from onix_ft.transport.base import StubTransport


class LossyTransport(StubTransport):
    """
    Стаб-транспорт, теряющий выбранные сообщения ПРИ ОТПРАВКЕ.

    `drop` — предикат (тип кадра, seq, номер попытки) → терять ли сообщение.
    Номер попытки считается по каждому (тип, seq) отдельно, поэтому легко
    задать «блок 2 теряется только в первый раз».
    """

    def __init__(self, inbox, outbox, drop=None):
        super().__init__(inbox, outbox)
        self._drop = drop or (lambda *_: False)
        self._attempts: dict = {}
        self.dropped: list = []

    def send(self, text: str) -> None:
        frame = Frame.decode(text)
        if frame is not None:
            key = (frame.type, frame.seq)
            self._attempts[key] = self._attempts.get(key, 0) + 1
            if self._drop(frame.type, frame.seq, self._attempts[key]):
                self.dropped.append(key)
                return  # сообщение «не доехало» — молча, как в реальном канале
        super().send(text)


def _count_data(messages: list) -> int:
    """Сколько DATA-сообщений реально ушло в канал (цена передачи)."""
    total = 0
    for text in messages:
        try:
            frame = Frame.decode(text)
        except Exception:
            continue
        if frame is not None and frame.type == FrameType.DATA:
            total += 1
    return total


def _transfer(tmp_path: Path, size_bytes: int, drop, window: int = 3, timeout: float = 60.0):
    """Прогнать передачу через теряющий канал; вернуть (ok, путь, потери)."""
    src = tmp_path / "источник.bin"
    src.write_bytes(os.urandom(size_bytes))
    out_dir  = tmp_path / "received"
    ckpt_dir = tmp_path / "ckpt"
    out_dir.mkdir(); ckpt_dir.mkdir()

    s2r: list = []
    r2s: list = []
    sender_transport   = LossyTransport(inbox=r2s, outbox=s2r, drop=drop)
    receiver_transport = StubTransport(inbox=s2r, outbox=r2s)

    holder: dict = {}

    def run_receiver():
        recv = FileReceiver(receiver_transport, out_dir=out_dir, ckpt_dir=ckpt_dir)
        holder["path"] = recv.receive_file()

    thread = threading.Thread(target=run_receiver, daemon=True)
    thread.start()
    time.sleep(0.1)

    started = time.monotonic()
    ok = FileSender(sender_transport, ckpt_dir=ckpt_dir).send_file(src)
    thread.join(timeout=timeout)
    elapsed = time.monotonic() - started

    stats = {
        "elapsed":   elapsed,
        "data_sent": _count_data(s2r),
        "dropped":   len(sender_transport.dropped),
        "blocks":    (size_bytes + config.CHUNK_BYTES - 1) // config.CHUNK_BYTES,
    }
    print(f"  цена передачи: {stats}")
    return ok, holder.get("path"), sender_transport.dropped, file_sha256(src), stats


@pytest.fixture(autouse=True)
def fast_protocol(monkeypatch):
    """Ускоряем протокол: тесты не должны ждать реальные таймауты."""
    monkeypatch.setattr(config, "WINDOW_SIZE", 3)
    monkeypatch.setattr(config, "SEND_DELAY", 0.0)
    monkeypatch.setattr(config, "POLL_INTERVAL", 0.01)
    # raising=False — тот же тест должен запускаться и на коде ДО правки,
    # где параметра подталкивания ещё не существует (проверка базлайна)
    monkeypatch.setattr(config, "IDLE_NUDGE_SECONDS", 1.0, raising=False)
    monkeypatch.setattr(config, "ACK_TIMEOUT", 8.0)
    monkeypatch.setattr(config, "BLOCK_WAIT_TIMEOUT", 20.0)
    monkeypatch.setattr(config, "META_WAIT_TIMEOUT", 20.0)
    monkeypatch.setattr(config, "CLEAR_CHAT_EVERY_N_BLOCKS", 0)


def test_lost_block_in_the_middle_of_window(tmp_path):
    """Потерян блок в середине окна: следующие приходят вперёд — их не выбрасываем."""
    def drop(ftype, seq, attempt):
        return ftype == FrameType.DATA and seq == 1 and attempt == 1

    ok, path, dropped, src_sha, stats = _transfer(tmp_path, 12_000, drop)

    assert dropped, "тест обязан был потерять сообщение"
    assert ok, "отправитель не довёз файл через теряющий канал"
    assert path is not None, "получатель не собрал файл"
    assert file_sha256(path) == src_sha


def test_lost_last_block_of_window_recovers_by_nudge(tmp_path, monkeypatch):
    """
    Потерян ПОСЛЕДНИЙ блок окна — после него не приходит ничего.

    Прежний приёмник здесь молчал до BLOCK_WAIT_TIMEOUT и умирал: NACK слался
    только при виде внеочередного блока. Теперь спасает подталкивание по
    тишине (IDLE_NUDGE_SECONDS).
    """
    monkeypatch.setattr(config, "ACK_TIMEOUT", 30.0)   # как в бою: ждать долго

    def drop(ftype, seq, attempt):
        return ftype == FrameType.DATA and seq == 2 and attempt == 1   # окно [0..2]

    ok, path, dropped, src_sha, stats = _transfer(tmp_path, 12_000, drop, timeout=90.0)

    assert dropped
    assert ok and path is not None
    assert file_sha256(path) == src_sha
    # Ключевое: восстановление идёт по тишине (~1 сек), а не по ACK_TIMEOUT
    # отправителя. До правки здесь уходило всё ожидание подтверждения целиком.
    assert stats["elapsed"] < 15.0, f"восстановление затянулось: {stats}"


def test_lost_ack_recovers(tmp_path):
    """Потеря подтверждения не должна ронять передачу (потери симметричны)."""
    def drop(ftype, seq, attempt):
        return ftype == FrameType.ACK and attempt == 1 and seq >= 0

    ok, path, dropped, src_sha, stats = _transfer(tmp_path, 12_000, drop)

    assert ok and path is not None
    assert file_sha256(path) == src_sha


def test_heavy_loss_every_third_message(tmp_path):
    """
    Фоновые потери около 15–30 %: передача обязана доехать, а не сорваться.
    Это модель реального канала из инцидента.
    """
    counter = {"n": 0}

    def drop(ftype, seq, attempt):
        if ftype != FrameType.DATA or attempt > 1:
            return False
        counter["n"] += 1
        return counter["n"] % 3 == 0

    ok, path, dropped, src_sha, stats = _transfer(tmp_path, 30_000, drop, timeout=120.0)

    assert len(dropped) >= 3, f"ожидали заметные потери, потеряно {len(dropped)}"
    assert ok and path is not None
    assert file_sha256(path) == src_sha
    # Цена: каждая потеря стоит ОДНОГО повтора, а не повтора всего окна.
    # Внеочередные блоки сохраняются, поэтому пересылок немногим больше,
    # чем блоков + потерь (до правки окно гонялось целиком по кругу).
    limit = stats["blocks"] + 3 * stats["dropped"]
    assert stats["data_sent"] <= limit, f"слишком много пересылок: {stats}, предел {limit}"


def test_out_of_order_block_is_kept_not_discarded(tmp_path):
    """
    Прямая проверка нового поведения: блок, пришедший вперёд, сохраняется
    в чекпойнте получателя, а не выбрасывается.
    """
    def drop(ftype, seq, attempt):
        return ftype == FrameType.DATA and seq == 0 and attempt == 1

    ok, path, dropped, src_sha, stats = _transfer(tmp_path, 12_000, drop)
    assert ok and path is not None and file_sha256(path) == src_sha


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
