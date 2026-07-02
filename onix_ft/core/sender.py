"""
Конечный автомат отправителя.

Алгоритм:
  1. Читать/создать чекпойнт.
  2. Отправить META, ждать ACK(META).
  3. Для каждого блока начиная с last_acked+1:
       а. Отправить DATA[seq].
       б. Ждать ACK[seq] или NACK[seq].
       в. NACK или таймаут → повторить (до MAX_RETRIES раз).
  4. Получить DONE от получателя, сверить sha256.
  5. Удалить чекпойнт.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .checkpoint import SenderCheckpoint
from .protocol   import (
    Frame, FrameType, FrameDecodeError,
    file_sha256, split_file,
    make_file_id, make_meta_frame, make_data_frame, make_abort_frame,
)
from ..transport.base import BaseTransport

logger = logging.getLogger("onix_ft.sender")

# ── настройки ────────────────────────────────────────────────────────────────

MAX_RETRIES:   int   = 5      # сколько раз повторить блок при NACK/таймауте
ACK_TIMEOUT:   float = 120.0  # секунд ждать ACK на каждый блок
POLL_INTERVAL: float = 2.0    # секунд между опросами transport.poll_new_messages()


# ── отправитель ──────────────────────────────────────────────────────────────

class FileSender:

    def __init__(self, transport: BaseTransport, ckpt_dir: Optional[Path] = None):
        self._t        = transport
        self._ckpt_dir = ckpt_dir or Path(".")
        # Буфер входящих кадров: кадры, пришедшие "не вовремя" (например, DONE
        # пришёл пока мы ещё обрабатывали последний ACK), чтобы не потерять их.
        self._frame_buf: list[Frame] = []

    def send_file(self, source_path: Path) -> bool:
        """
        Передать файл. Возвращает True при успехе, False при ошибке/прерывании.
        При перезапуске продолжает с последнего подтверждённого блока.
        """
        source_path = source_path.resolve()
        ckpt_path   = self._ckpt_dir / f"{source_path.name}.sender.ckpt.json"

        # ── восстановление или новая сессия ──────────────────────────────────
        cp = SenderCheckpoint.load(ckpt_path)
        if cp:
            logger.info(
                "Найден чекпойнт. Продолжаем передачу %s "
                "с блока %d из %d (file_id=%s).",
                source_path.name, cp.next_seq, cp.total_blocks, cp.file_id
            )
        else:
            logger.info("Новая передача: %s", source_path.name)
            sha256  = file_sha256(source_path)
            blocks  = split_file(source_path)
            file_id = make_file_id()

            cp = SenderCheckpoint(ckpt_path)
            cp.file_id      = file_id
            cp.source_path  = str(source_path)
            cp.sha256       = sha256
            cp.total_blocks = len(blocks)
            cp.save()

        # Разбиваем файл на блоки (нужны всегда — даже при восстановлении)
        blocks = split_file(source_path)
        assert len(blocks) == cp.total_blocks, "Файл изменился с момента чекпойнта!"

        logger.info(
            "Файл: %s | %d байт | %d блоков | file_id=%s",
            source_path.name, source_path.stat().st_size, cp.total_blocks, cp.file_id
        )

        # ── шаг 1: META ──────────────────────────────────────────────────────
        if not cp.meta_acked:
            logger.info("Отправка META...")
            meta_frame = make_meta_frame(cp.file_id, source_path, cp.total_blocks, cp.sha256)
            ok = self._send_and_wait_ack(
                frame        = meta_frame,
                expected_seq = 0,    # ACK на META: получатель шлёт ACK seq=0
                file_id      = cp.file_id,
                is_meta      = True,
            )
            if not ok:
                return False
            cp.confirm_meta()
            logger.info("META подтверждена.")

        # ── шаг 2: DATA блоки ────────────────────────────────────────────────
        start_seq = cp.next_seq
        for seq in range(start_seq, cp.total_blocks):
            data_frame = make_data_frame(cp.file_id, seq, cp.total_blocks, blocks[seq])

            logger.info(
                "Блок %d/%d (%d байт b64=%d символов)...",
                seq + 1, cp.total_blocks,
                len(blocks[seq]), len(data_frame.payload)
            )

            ok = self._send_and_wait_ack(
                frame        = data_frame,
                expected_seq = seq,
                file_id      = cp.file_id,
            )
            if not ok:
                logger.error("Не удалось передать блок %d после %d попыток. Прерываем.", seq, MAX_RETRIES)
                self._send_abort(cp.file_id, f"Блок {seq} не подтверждён после {MAX_RETRIES} попыток")
                return False

            cp.confirm_block(seq)

        # ── шаг 3: ожидание DONE от получателя ───────────────────────────────
        logger.info("Все блоки отправлены. Ожидаем DONE от получателя...")
        done_frame = self._wait_for_frame(
            expected_type = FrameType.DONE,
            file_id       = cp.file_id,
            timeout       = ACK_TIMEOUT,
        )
        if done_frame is None:
            logger.error("DONE не получен — таймаут.")
            return False

        if done_frame.payload != cp.sha256:
            logger.error(
                "SHA256 не совпадает! Ожидали %s, получатель сообщил %s",
                cp.sha256, done_frame.payload
            )
            return False

        logger.info("✓ Передача завершена успешно. SHA256 совпадает.")
        cp.delete()
        return True

    # ── вспомогательные методы ───────────────────────────────────────────────

    def _send_and_wait_ack(
        self,
        frame: Frame,
        expected_seq: int,
        file_id: str,
        is_meta: bool = False,
    ) -> bool:
        """Отправить кадр, ждать ACK. При NACK/таймауте — повторить. True = успех."""
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                logger.warning("  Повтор %d/%d...", attempt, MAX_RETRIES)

            self._t.send(frame.encode())

            # Ожидаем ACK с нужным seq
            ack = self._wait_for_frame(
                expected_type  = FrameType.ACK,
                file_id        = file_id,
                timeout        = ACK_TIMEOUT,
                expected_seq   = expected_seq if not is_meta else None,
                also_accept    = FrameType.NACK,
            )

            if ack is None:
                logger.warning("  Таймаут ACK (seq=%d).", expected_seq)
                continue

            if ack.type == FrameType.ACK:
                # Для META получатель шлёт ACK с seq=-1
                if is_meta or ack.seq == expected_seq:
                    return True
                else:
                    logger.warning("  ACK с неожиданным seq=%d (ожидали %d).", ack.seq, expected_seq)

            elif ack.type == FrameType.NACK:
                logger.warning("  Получен NACK seq=%d.", ack.seq)

        return False

    def _wait_for_frame(
        self,
        expected_type: FrameType,
        file_id: str,
        timeout: float,
        expected_seq: Optional[int] = None,
        also_accept: Optional[FrameType] = None,
    ) -> Optional[Frame]:
        """
        Блокирующий опрос: ждать кадр нужного типа, игнорируя
        обычные сообщения чата и чужие кадры.
        Буферизует кадры, пришедшие «не вовремя», чтобы не потерять их.
        """
        def _matches(frame: Frame) -> bool:
            if frame.type == expected_type:
                return expected_seq is None or frame.seq == expected_seq
            if also_accept and frame.type == also_accept:
                return True
            return False

        # Сначала проверяем буфер (кадры, пришедшие раньше)
        for i, frame in enumerate(self._frame_buf):
            if _matches(frame):
                self._frame_buf.pop(i)
                return frame

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Забираем все новые сообщения разом и обрабатываем полный батч.
            # Нужный кадр может оказаться не первым (например, DONE пришёл
            # в том же батче, что и последний ACK).
            batch = list(self._t.poll_new_messages())
            found: Optional[Frame] = None
            for raw in batch:
                frame = self._try_decode(raw, file_id)
                if frame is None:
                    continue
                if frame.type == FrameType.ABORT:
                    logger.error("Получен ABORT от получателя: %s", frame.payload)
                    return None
                if found is None and _matches(frame):
                    found = frame  # запоминаем первый подходящий
                else:
                    # Остальные — в буфер, чтобы не потерять
                    logger.debug(
                        "Буферизуем кадр %s seq=%d (ждём %s)",
                        frame.type, frame.seq, expected_type
                    )
                    self._frame_buf.append(frame)
            if found is not None:
                return found
            time.sleep(POLL_INTERVAL)
        return None

    def _try_decode(self, raw: str, file_id: str) -> Optional[Frame]:
        try:
            frame = Frame.decode(raw)
            if frame is None:
                return None  # обычное сообщение чата
            if frame.file_id != file_id:
                return None  # кадр другой сессии
            return frame
        except FrameDecodeError as e:
            logger.warning("Битый кадр (игнорируем): %s", e)
            return None

    def _send_abort(self, file_id: str, reason: str):
        try:
            self._t.send(make_abort_frame(file_id, reason).encode())
        except Exception:
            pass
