"""
Тест полного цикла: отправка + приём через StubTransport (без браузера).

Запуск:
    python -m pytest onix_ft/tests/ -v
или
    python onix_ft/tests/test_protocol_roundtrip.py
"""

import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft.core.protocol import (
    Frame, FrameDecodeError, make_file_id,
    make_meta_frame, make_data_frame, make_ack_frame,
    file_sha256, split_file, CHUNK_BYTES,
)
from onix_ft.core.sender    import FileSender
from onix_ft.core.receiver  import FileReceiver
from onix_ft.transport.base import StubTransport


# ── тест 1: codec кадра ──────────────────────────────────────────────────────

def test_frame_encode_decode():
    file_id = make_file_id()
    frame   = make_ack_frame(file_id, seq=42)
    text    = frame.encode()
    assert text.startswith("##FT|v1|")
    assert text.endswith("##")

    decoded = Frame.decode(text)
    assert decoded is not None
    assert decoded.type.value == "ACK"
    assert decoded.seq == 42
    assert decoded.file_id == file_id
    print(f"  [OK] encode/decode ACK: {text[:80]}...")


def test_frame_crc_mismatch():
    frame = make_ack_frame(make_file_id(), seq=1)
    text  = frame.encode()
    # Портим один символ внутри
    corrupted = text[:20] + ("X" if text[20] != "X" else "Y") + text[21:]
    try:
        Frame.decode(corrupted)
        assert False, "Должна была быть FrameDecodeError"
    except FrameDecodeError:
        pass
    print("  [OK] CRC mismatch обнаружен")


def test_regular_message_ignored():
    result = Frame.decode("Привет, как дела?")
    assert result is None
    result2 = Frame.decode("Hello world")
    assert result2 is None
    print("  [OK] Обычные сообщения игнорируются")


def test_frame_length():
    """DATA-кадр не должен превышать лимит сообщения."""
    raw_block = os.urandom(CHUNK_BYTES)
    frame     = make_data_frame(make_file_id(), seq=0, total=1, raw_block=raw_block)
    text      = frame.encode()
    assert len(text) <= 4000, f"Кадр слишком длинный: {len(text)}"
    print(f"  [OK] DATA-кадр: {len(text)} символов (≤4000)")


# ── тест 2: полный round-trip ─────────────────────────────────────────────────

def test_roundtrip(file_size_bytes: int = 12_000):
    print(f"\n  Round-trip: {file_size_bytes} байт ({file_size_bytes // CHUNK_BYTES + 1} блоков)")

    # Создаём временный файл с псевдослучайным содержимым
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        src_path = tmpdir / "test_requirements.md"
        src_path.write_bytes(os.urandom(file_size_bytes))
        src_sha256 = file_sha256(src_path)

        out_dir  = tmpdir / "received"
        ckpt_dir = tmpdir / "ckpt"
        out_dir.mkdir(); ckpt_dir.mkdir()

        # Два канала (очереди): sender→receiver и receiver→sender
        s2r: list[str] = []  # sender пишет, receiver читает
        r2s: list[str] = []  # receiver пишет, sender читает

        sender_transport   = StubTransport(inbox=r2s, outbox=s2r)
        receiver_transport = StubTransport(inbox=s2r, outbox=r2s)

        result_holder: dict = {}

        def run_receiver():
            recv = FileReceiver(receiver_transport, out_dir=out_dir, ckpt_dir=ckpt_dir)
            result_holder["path"] = recv.receive_file()

        recv_thread = threading.Thread(target=run_receiver, daemon=True)
        recv_thread.start()

        time.sleep(0.1)  # дать получателю стартовать первым

        sender = FileSender(sender_transport, ckpt_dir=ckpt_dir)
        ok     = sender.send_file(src_path)

        recv_thread.join(timeout=30)

        assert ok, "Отправитель завершился с ошибкой"
        assert result_holder.get("path") is not None, "Получатель не принял файл"

        received_sha256 = file_sha256(result_holder["path"])
        assert received_sha256 == src_sha256, (
            f"SHA256 не совпадает: {src_sha256} vs {received_sha256}"
        )
        print(f"  [OK] SHA256 совпадает: {src_sha256[:16]}...")
        print(f"  [OK] Файл сохранён: {result_holder['path']}")


# ── точка входа ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Тесты протокола OnixFT ===\n")

    print("[1] Codec тесты:")
    test_frame_encode_decode()
    test_frame_crc_mismatch()
    test_regular_message_ignored()
    test_frame_length()

    print("\n[2] Round-trip тесты:")
    test_roundtrip(file_size_bytes=1_000)          # 1 КБ
    test_roundtrip(file_size_bytes=12_000)         # ~12 КБ, несколько блоков
    test_roundtrip(file_size_bytes=100_000)        # 100 КБ

    print("\n✓ Все тесты пройдены.")
