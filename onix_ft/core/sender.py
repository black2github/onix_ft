"""
Конечный автомат отправителя — оконный протокол (sliding window).

Алгоритм:
  1. Читать/создать чекпойнт.
  2. Отправить META, ждать ACK(META).
  3. Передача окнами по WINDOW_SIZE блоков:
       а. Отправить W блоков подряд без ожидания ACK между ними.
       б. Ждать ACK[last] от получателя (подтверждение всего окна).
       в. При NACK[N] — откатиться к блоку N и повторить окно с него.
       г. При таймауте — повторить всё окно (до MAX_RETRIES раз).
       д. После каждых CLEAR_CHAT_EVERY_N_BLOCKS блоков — очистить чат.
  4. Получить DONE от получателя, сверить sha256.
  5. Удалить чекпойнт.

Совместимость: при WINDOW_SIZE=1 протокол деградирует до stop-and-wait,
полностью совместимого со старым receiver.
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
from .. import config

logger = logging.getLogger("onix_ft.sender")

# ── настройки ────────────────────────────────────────────────────────────────

MAX_RETRIES:   int   = 5      # сколько раз повторить окно при NACK/таймауте
ACK_TIMEOUT:   float = 120.0  # секунд ждать ACK на всё окно
POLL_INTERVAL: float = 2.0    # секунд между опросами transport.poll_new_messages()


# ── отправитель ──────────────────────────────────────────────────────────────

class FileSender:

    def __init__(self, transport: BaseTransport, ckpt_dir: Optional[Path] = None):
        self._t        = transport
        self._ckpt_dir = ckpt_dir or Path(".")
        # Буфер входящих кадров: кадры, пришедшие «не вовремя», чтобы не потерять.
        self._frame_buf: list[Frame] = []
        # Счётчик блоков, переданных с момента последней очистки чата.
        self._blocks_since_clear: int = 0

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
            # Проверяем что файл на диске не изменился с момента создания чекпойнта.
            actual_sha256 = file_sha256(source_path)
            if actual_sha256 != cp.sha256:
                logger.error(
                    "Файл %s изменился с момента создания чекпойнта! "
                    "SHA256 в чекпойнте: %s, SHA256 на диске: %s. "
                    "Удалите чекпойнт %s и запустите передачу заново.",
                    source_path.name, cp.sha256, actual_sha256, ckpt_path,
                )
                return False
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

        # Разбиваем файл на блоки (нужны всегда — даже при восстановлении).
        blocks = split_file(source_path)

        window_size = max(1, config.WINDOW_SIZE)
        logger.info(
            "Файл: %s | %d байт | %d блоков | file_id=%s | окно=%d",
            source_path.name, source_path.stat().st_size,
            cp.total_blocks, cp.file_id, window_size
        )

        # ── шаг 1: META ──────────────────────────────────────────────────────
        if not cp.meta_acked:
            logger.info("Отправка META...")
            meta_frame = make_meta_frame(
                cp.file_id, source_path, cp.total_blocks, cp.sha256
            )
            ok = self._send_and_wait_ack(
                frame        = meta_frame,
                expected_seq = 0,
                file_id      = cp.file_id,
                is_meta      = True,
            )
            if not ok:
                return False
            cp.confirm_meta()
            logger.info("META подтверждена.")

        # ── шаг 2: DATA блоки окнами ─────────────────────────────────────────
        seq = cp.next_seq
        while seq < cp.total_blocks:
            # Границы текущего окна
            win_start = seq
            win_end   = min(seq + window_size, cp.total_blocks) - 1  # включительно

            ok, next_seq = self._send_window(
                blocks    = blocks,
                win_start = win_start,
                win_end   = win_end,
                cp        = cp,
                file_id   = cp.file_id,
            )
            if not ok:
                self._send_abort(
                    cp.file_id,
                    f"Окно [{win_start}..{win_end}] не подтверждено после {MAX_RETRIES} попыток"
                )
                return False

            seq = next_seq

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

        logger.info("Передача завершена успешно. SHA256 совпадает.")
        cp.delete()
        return True

    # ── передача одного окна ─────────────────────────────────────────────────

    def _send_window(
        self,
        blocks:    list[bytes],
        win_start: int,
        win_end:   int,
        cp:        SenderCheckpoint,
        file_id:   str,
    ) -> tuple[bool, int]:
        """
        Отправить окно блоков [win_start..win_end] и дождаться ACK.

        Возвращает (True, next_seq) при успехе или (False, win_start) при ошибке.

        При получении NACK[N] — откатываемся к блоку N внутри текущей попытки.
        Полный откат (повтор всего окна) происходит при таймауте или исчерпании
        внутренних попыток отката.
        """
        logger.info(
            "Окно [%d..%d] из %d блоков...",
            win_start, win_end, cp.total_blocks
        )

        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                logger.warning("  Повтор окна [%d..%d], попытка %d/%d...",
                               win_start, win_end, attempt, MAX_RETRIES)

            # Отправляем все блоки окна подряд без ожидания ACK
            send_from = win_start if attempt == 1 else win_start
            for seq in range(send_from, win_end + 1):
                frame = make_data_frame(file_id, seq, cp.total_blocks, blocks[seq])
                logger.debug(
                    "  → DATA[%d/%d] (%d байт)",
                    seq + 1, cp.total_blocks, len(blocks[seq])
                )
                self._t.send(frame.encode())

            # Ждём ACK на последний блок окна (или NACK на любой блок)
            response = self._wait_for_frame(
                expected_type = FrameType.ACK,
                file_id       = file_id,
                timeout       = ACK_TIMEOUT,
                expected_seq  = win_end,
                also_accept   = FrameType.NACK,
            )

            if response is None:
                logger.warning("  Таймаут ожидания ACK[%d].", win_end)
                continue  # повторяем всё окно

            if response.type == FrameType.NACK:
                nack_seq = response.seq
                logger.warning(
                    "  NACK[%d] — получатель запросил повтор с блока %d.",
                    nack_seq, nack_seq
                )
                # Откатываемся к блоку nack_seq внутри окна
                win_start = nack_seq
                continue

            if response.type == FrameType.ACK:
                acked_seq = response.seq
                if acked_seq < win_end:
                    # Получатель подтвердил только часть окна — принимаем как есть,
                    # следующее окно начнём с acked_seq + 1
                    logger.info(
                        "  ACK[%d] (частичное подтверждение, ожидали %d).",
                        acked_seq, win_end
                    )
                else:
                    logger.info("  ACK[%d] ✓", acked_seq)

                # Подтверждаем все блоки окна до acked_seq включительно
                for seq in range(cp.next_seq, acked_seq + 1):
                    cp.confirm_block(seq)
                    self._blocks_since_clear += 1

                # Периодическая очистка чата
                self._maybe_clear_chat(next_seq=acked_seq + 1, total=cp.total_blocks)

                return True, acked_seq + 1

        # Все попытки исчерпаны
        logger.error(
            "Окно [%d..%d] не подтверждено после %d попыток.",
            win_start, win_end, MAX_RETRIES
        )
        return False, win_start

    # ── периодическая очистка чата ───────────────────────────────────────────

    def _maybe_clear_chat(self, next_seq: int, total: int):
        """
        Очистить чат если передано достаточно блоков и это не последний блок.
        """
        n = config.CLEAR_CHAT_EVERY_N_BLOCKS
        if n <= 0 or self._blocks_since_clear < n:
            return
        if next_seq >= total:
            return  # последний блок — не чистим перед ожиданием DONE
        logger.info(
            "Передано %d блоков — очищаем чат перед продолжением...",
            self._blocks_since_clear
        )
        self._try_clear_chat()
        self._blocks_since_clear = 0

    # ── вспомогательные методы ───────────────────────────────────────────────

    def _send_and_wait_ack(
        self,
        frame: Frame,
        expected_seq: int,
        file_id: str,
        is_meta: bool = False,
    ) -> bool:
        """Отправить одиночный кадр (META), ждать ACK. True = успех."""
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                logger.warning("  Повтор %d/%d...", attempt, MAX_RETRIES)

            self._t.send(frame.encode())

            ack = self._wait_for_frame(
                expected_type = FrameType.ACK,
                file_id       = file_id,
                timeout       = ACK_TIMEOUT,
                expected_seq  = expected_seq if not is_meta else None,
                also_accept   = FrameType.NACK,
            )

            if ack is None:
                logger.warning("  Таймаут ACK (seq=%d).", expected_seq)
                continue

            if ack.type == FrameType.ACK:
                if is_meta or ack.seq == expected_seq:
                    return True
                logger.warning(
                    "  ACK с неожиданным seq=%d (ожидали %d).", ack.seq, expected_seq
                )
            elif ack.type == FrameType.NACK:
                logger.warning("  Получен NACK seq=%d.", ack.seq)

        return False

    def _wait_for_frame(
        self,
        expected_type: FrameType,
        file_id: str,
        timeout: float,
        expected_seq:  Optional[int]       = None,
        also_accept:   Optional[FrameType] = None,
    ) -> Optional[Frame]:
        """
        Блокирующий опрос: ждать кадр нужного типа.
        Буферизует «внеочередные» кадры, чтобы не потерять их.
        """
        def _matches(frame: Frame) -> bool:
            if frame.type == expected_type:
                return expected_seq is None or frame.seq == expected_seq
            if also_accept and frame.type == also_accept:
                return True
            return False

        # Сначала проверяем буфер
        for i, frame in enumerate(self._frame_buf):
            if _matches(frame):
                self._frame_buf.pop(i)
                return frame

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
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
                    found = frame
                else:
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
                return None
            if frame.file_id != file_id:
                return None
            return frame
        except FrameDecodeError as e:
            logger.warning("Битый кадр (игнорируем): %s", e)
            return None

    def _try_clear_chat(self):
        """
        Попытаться очистить историю чата через транспорт.
        Ошибка очистки не прерывает передачу — только логируется.
        """
        if not hasattr(self._t, 'clear_chat_history'):
            logger.debug("Транспорт не поддерживает clear_chat_history — пропускаем.")
            return
        try:
            ok = self._t.clear_chat_history()
            if ok:
                logger.info("Чат очищён.")
            else:
                logger.warning("Не удалось очистить чат — продолжаем без очистки.")
        except Exception as e:
            logger.warning("Ошибка при очистке чата: %s — продолжаем.", e)

    def _send_abort(self, file_id: str, reason: str):
        try:
            self._t.send(make_abort_frame(file_id, reason).encode())
        except Exception:
            pass