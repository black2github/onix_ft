"""
Конечный автомат получателя.

Алгоритм:
  1. Ждать кадр META — из него узнаём имя файла, размер, sha256, число блоков.
     Подтвердить ACK(seq=-1).
  2. Ждать DATA-блоки в любом порядке (stop-and-wait с нашей стороны означает,
     что блоки придут строго последовательно, но на всякий случай обрабатываем
     дубликаты идемпотентно).
     На каждый блок: сохранить, отправить ACK. При ошибке CRC протокола — NACK.
  3. После получения всех блоков: собрать файл, проверить sha256, отправить DONE.
  4. Удалить чекпойнт.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from .checkpoint import ReceiverCheckpoint
from .protocol   import (
    Frame, FrameType, FrameDecodeError,
    decode_data_payload, make_ack_frame, make_nack_frame,
    make_done_frame, make_abort_frame,
)
from ..transport.base import BaseTransport

logger = logging.getLogger("onix_ft.receiver")

# ── настройки ────────────────────────────────────────────────────────────────

META_WAIT_TIMEOUT: float = 3600.0  # ждём META (может, отправитель ещё не запустился)
BLOCK_WAIT_TIMEOUT: float = 120.0  # ждём каждый DATA-блок
POLL_INTERVAL: float = 2.0


class FileReceiver:

    def __init__(
        self,
        transport: BaseTransport,
        out_dir: Path,
        ckpt_dir: Optional[Path] = None,
    ):
        self._t        = transport
        self._out_dir  = out_dir.resolve()
        self._ckpt_dir = ckpt_dir or Path(".")
        self._out_dir.mkdir(parents=True, exist_ok=True)

    def receive_file(self) -> Optional[Path]:
        """
        Ждать и принять один файл.
        Возвращает путь к принятому файлу или None при ошибке.
        """

        # ── шаг 1: ждём META ─────────────────────────────────────────────────
        logger.info("Ожидание META от отправителя (до %.0f сек)...", META_WAIT_TIMEOUT)
        meta_frame, cp = self._wait_for_meta()
        if meta_frame is None:
            logger.error("META не получена — таймаут.")
            return None

        # Отправляем ACK на META (seq=0, тип META_ACK условно кодируем как ACK с seq=0)
        self._t.send(make_ack_frame(cp.file_id, seq=0).encode())
        logger.info(
            "META принята: файл=%s, размер=%d байт, блоков=%d, file_id=%s",
            cp.filename, cp.file_size, cp.total_blocks, cp.file_id
        )

        # Буфер для блоков: номер_блока → байты
        # Если чекпойнт загружен — уже принятые блоки восстанавливаем с диска
        block_buf = self._restore_partial_blocks(cp)

        # ── шаг 2: принимаем DATA-блоки ──────────────────────────────────────
        while not cp.is_complete:
            missing = cp.missing_blocks
            logger.info(
                "Принято %d/%d блоков. Жду следующий (seq=%d)...",
                len(cp.received), cp.total_blocks, missing[0]
            )

            frame = self._wait_for_frame(
                expected_type = FrameType.DATA,
                file_id       = cp.file_id,
                timeout       = BLOCK_WAIT_TIMEOUT,
            )

            if frame is None:
                logger.error(
                    "Таймаут ожидания блока seq=%d. "
                    "Отправитель, вероятно, завис. Прерываем.",
                    missing[0]
                )
                self._send_abort(cp.file_id, f"Таймаут блока seq={missing[0]}")
                return None

            seq = frame.seq

            # Дубликат — уже получили этот блок
            if seq in cp.received:
                logger.debug("Дубликат блока seq=%d, повторно подтверждаем.", seq)
                self._t.send(make_ack_frame(cp.file_id, seq).encode())
                continue

            # Декодируем payload
            try:
                raw = decode_data_payload(frame.payload)
            except Exception as e:
                logger.warning("Ошибка декодирования блока seq=%d: %s — NACK.", seq, e)
                self._t.send(make_nack_frame(cp.file_id, seq).encode())
                continue

            # Сохраняем блок и обновляем чекпойнт
            block_buf[seq] = raw
            self._save_partial_block(cp, seq, raw)
            cp.mark_received(seq)

            self._t.send(make_ack_frame(cp.file_id, seq).encode())
            logger.debug("ACK → seq=%d ✓", seq)

        # ── шаг 3: сборка файла ───────────────────────────────────────────────
        logger.info("Все блоки получены. Собираем файл...")
        out_path = self._assemble_file(cp, block_buf)

        # ── шаг 4: проверка sha256 ────────────────────────────────────────────
        actual_sha256 = _file_sha256(out_path)
        if actual_sha256 != cp.sha256:
            logger.error(
                "SHA256 не совпадает! Ожидали %s, получили %s. Файл повреждён.",
                cp.sha256, actual_sha256
            )
            self._send_abort(cp.file_id, "SHA256 mismatch после сборки")
            return None

        # ── шаг 5: DONE ───────────────────────────────────────────────────────
        self._t.send(make_done_frame(cp.file_id, actual_sha256).encode())
        logger.info("✓ Файл принят и проверен: %s", out_path)

        # Чистим временные файлы блоков
        self._cleanup_partial_blocks(cp)
        cp.delete()

        return out_path

    # ── ожидание META ─────────────────────────────────────────────────────────

    def _wait_for_meta(self) -> tuple[Optional[Frame], Optional[ReceiverCheckpoint]]:
        """
        Ждём META. Если есть незавершённый чекпойнт — возвращаем его
        вместо ожидания новой META (режим восстановления).
        """
        # Проверяем существующие чекпойнты в ckpt_dir
        for ckpt_path in self._ckpt_dir.glob("*.receiver.ckpt.json"):
            cp = ReceiverCheckpoint.load(ckpt_path)
            if cp and not cp.is_complete:
                logger.info(
                    "Найден незавершённый чекпойнт: %s (%d/%d блоков). Продолжаем.",
                    cp.filename, len(cp.received), cp.total_blocks
                )
                # Создаём фиктивный META-frame для совместимости возвращаемого типа
                fake_meta = Frame(
                    type    = FrameType.META,
                    file_id = cp.file_id,
                    seq     = 0,
                    total   = cp.total_blocks,
                    payload = "",  # не нужен — уже всё в cp
                )
                return fake_meta, cp

        # Ждём настоящую META из канала
        deadline = time.monotonic() + META_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            for raw in self._t.poll_new_messages():
                frame = self._try_decode_any(raw)
                if frame is None:
                    continue
                if frame.type == FrameType.META:
                    cp = self._init_checkpoint(frame)
                    return frame, cp
            time.sleep(POLL_INTERVAL)

        return None, None

    def _init_checkpoint(self, meta: Frame) -> ReceiverCheckpoint:
        meta_data = json.loads(meta.payload)
        ckpt_path = self._ckpt_dir / f"{meta.file_id}.receiver.ckpt.json"
        cp = ReceiverCheckpoint(ckpt_path)
        cp.file_id      = meta.file_id
        cp.filename     = meta_data["name"]
        cp.sha256       = meta_data["sha256"]
        cp.total_blocks = meta_data["blocks"]
        cp.file_size    = meta_data.get("size", 0)
        cp.out_dir      = str(self._out_dir)
        cp.save()
        return cp

    # ── ожидание кадра ───────────────────────────────────────────────────────

    def _wait_for_frame(
        self,
        expected_type: FrameType,
        file_id: str,
        timeout: float,
    ) -> Optional[Frame]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for raw in self._t.poll_new_messages():
                frame = self._try_decode_any(raw)
                if frame is None:
                    continue
                if frame.file_id != file_id:
                    continue
                if frame.type == expected_type:
                    return frame
                if frame.type == FrameType.ABORT:
                    logger.error("ABORT от отправителя: %s", frame.payload)
                    return None
            time.sleep(POLL_INTERVAL)
        return None

    # ── хранение частичных блоков ────────────────────────────────────────────

    def _partial_block_path(self, cp: ReceiverCheckpoint, seq: int) -> Path:
        return self._ckpt_dir / f"{cp.file_id}.block.{seq:06d}.bin"

    def _save_partial_block(self, cp: ReceiverCheckpoint, seq: int, data: bytes):
        self._partial_block_path(cp, seq).write_bytes(data)

    def _restore_partial_blocks(self, cp: ReceiverCheckpoint) -> dict[int, bytes]:
        buf: dict[int, bytes] = {}
        for seq in cp.received:
            p = self._partial_block_path(cp, seq)
            if p.exists():
                buf[seq] = p.read_bytes()
            else:
                logger.warning("Блок seq=%d помечен как принятый, но файл отсутствует. Сбрасываем.", seq)
                cp.received.discard(seq)
        return buf

    def _cleanup_partial_blocks(self, cp: ReceiverCheckpoint):
        for seq in range(cp.total_blocks):
            p = self._partial_block_path(cp, seq)
            if p.exists():
                p.unlink()

    # ── сборка финального файла ──────────────────────────────────────────────

    def _assemble_file(self, cp: ReceiverCheckpoint, block_buf: dict[int, bytes]) -> Path:
        out_path = self._out_dir / cp.filename
        # Если файл уже существует — добавим суффикс, чтобы не перезаписать
        if out_path.exists():
            out_path = self._out_dir / f"{cp.file_id}_{cp.filename}"

        with open(out_path, "wb") as f:
            for seq in range(cp.total_blocks):
                f.write(block_buf[seq])

        logger.info("Файл записан: %s (%d байт)", out_path, out_path.stat().st_size)
        return out_path

    # ── утилиты ──────────────────────────────────────────────────────────────

    def _try_decode_any(self, raw: str) -> Optional[Frame]:
        try:
            return Frame.decode(raw)
        except FrameDecodeError as e:
            logger.warning("Битый кадр (игнорируем): %s", e)
            return None

    def _send_abort(self, file_id: str, reason: str):
        try:
            self._t.send(make_abort_frame(file_id, reason).encode())
        except Exception:
            pass


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
