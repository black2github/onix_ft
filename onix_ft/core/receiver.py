"""
Конечный автомат получателя — оконный протокол (sliding window).

Алгоритм получателя прост и не зависит от размера окна отправителя:
  - Принимаем блоки по одному (как они приходят).
  - ACK шлём на каждый WINDOW_SIZE-й блок (или на последний).
  - При пропуске блока — NACK с номером первого недостающего.

Отправитель управляет размером окна сам. Receiver только подтверждает
накопленные блоки пакетом, уменьшая число сообщений в чате в WINDOW_SIZE раз.
"""

from __future__ import annotations

import hashlib
import zipfile
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
from .. import config

logger = logging.getLogger("onix_ft.receiver")

# Настройки вынесены в config.py (config.META_WAIT_TIMEOUT, config.BLOCK_WAIT_TIMEOUT, config.POLL_INTERVAL).


class FileReceiver:

    def __init__(
        self,
        transport: BaseTransport,
        out_dir:   Path,
        ckpt_dir:  Optional[Path] = None,
    ):
        self._t        = transport
        self._out_dir  = out_dir.resolve()
        self._ckpt_dir = ckpt_dir or Path(".")
        self._out_dir.mkdir(parents=True, exist_ok=True)
        # Буфер DATA-кадров: poll может вернуть несколько блоков за раз,
        # а мы обрабатываем их по одному — остаток сохраняем здесь.
        self._data_buf: list[Frame] = []

    def receive_file(self) -> Optional[Path]:
        """Принять один файл. Возвращает путь или None при ошибке."""

        # ── шаг 1: META ──────────────────────────────────────────────────────
        logger.info(
            "Ожидание META от отправителя (до %.0f сек)...", config.META_WAIT_TIMEOUT
        )
        meta_frame, cp = self._wait_for_meta()
        if meta_frame is None:
            logger.error("META не получена — таймаут.")
            return None

        self._t.send(make_ack_frame(cp.file_id, seq=0).encode())
        logger.info(
            "META принята: файл=%s, размер=%d байт, блоков=%d, file_id=%s",
            cp.filename, cp.file_size, cp.total_blocks, cp.file_id
        )

        block_buf        = self._restore_partial_blocks(cp)
        window_size      = max(1, config.WINDOW_SIZE)
        blocks_since_ack = 0   # сколько блоков принято с момента последнего ACK
        last_acked_seq   = -1  # seq последнего подтверждённого блока

        # ── шаг 2: принимаем DATA-блоки ──────────────────────────────────────
        while not cp.is_complete:
            missing      = cp.missing_blocks
            expected_seq = missing[0]

            logger.info(
                "Принято %d/%d блоков. Жду seq=%d...",
                len(cp.received), cp.total_blocks, expected_seq
            )

            frame = self._wait_for_data(cp.file_id, timeout=config.BLOCK_WAIT_TIMEOUT)

            if frame is None:
                logger.error("Таймаут ожидания блока seq=%d.", expected_seq)
                self._send_abort(cp.file_id, f"Таймаут блока seq={expected_seq}")
                return None

            seq = frame.seq

            # Дубликат
            if seq in cp.received:
                logger.debug("Дубликат seq=%d — подтверждаем повторно.", seq)
                self._t.send(make_ack_frame(cp.file_id, seq).encode())
                blocks_since_ack = 0
                continue

            # Неожиданный блок — пропуск
            if seq != expected_seq:
                logger.warning(
                    "Получен seq=%d, ожидали seq=%d → NACK[%d].",
                    seq, expected_seq, expected_seq
                )
                self._t.send(make_nack_frame(cp.file_id, expected_seq).encode())
                blocks_since_ack = 0
                continue

            # Декодируем
            try:
                raw = decode_data_payload(frame.payload)
            except Exception as e:
                logger.warning("Ошибка декодирования seq=%d: %s → NACK.", seq, e)
                self._t.send(make_nack_frame(cp.file_id, seq).encode())
                blocks_since_ack = 0
                continue

            # Сохраняем блок
            block_buf[seq] = raw
            self._save_partial_block(cp, seq, raw)
            cp.mark_received(seq)
            blocks_since_ack += 1
            last_acked_seq    = seq
            logger.debug("Блок seq=%d принят (%d байт).", seq, len(raw))

            # Шлём ACK если накопили window_size блоков или получили последний
            if blocks_since_ack >= window_size or cp.is_complete:
                logger.debug(
                    "ACK → seq=%d (принято %d блоков в окне).",
                    last_acked_seq, blocks_since_ack
                )
                self._t.send(make_ack_frame(cp.file_id, last_acked_seq).encode())
                blocks_since_ack = 0

        # ── шаг 3: сборка и проверка ─────────────────────────────────────────
        logger.info("Все блоки получены. Собираем файл...")
        out_path = self._assemble_file(cp, block_buf)

        actual_sha256 = _file_sha256(out_path)
        if actual_sha256 != cp.sha256:
            logger.error(
                "SHA256 не совпадает! Ожидали %s, получили %s.",
                cp.sha256, actual_sha256
            )
            self._send_abort(cp.file_id, "SHA256 mismatch после сборки")
            return None

        self._t.send(make_done_frame(cp.file_id, actual_sha256).encode())
        logger.info("Файл принят и проверен: %s", out_path)

        # Если файл является автоматически созданным архивом каталога —
        # распаковываем и удаляем архив. Обычные ZIP-файлы не трогаем.
        if cp.auto_extract:
            out_path = self._extract_archive(out_path)

        self._cleanup_partial_blocks(cp)
        cp.delete()
        return out_path

    # ── ожидание META ─────────────────────────────────────────────────────────

    def _wait_for_meta(
        self,
    ) -> tuple[Optional[Frame], Optional[ReceiverCheckpoint]]:
        for ckpt_path in self._ckpt_dir.glob("*.receiver.ckpt.json"):
            cp = ReceiverCheckpoint.load(ckpt_path)
            if cp and not cp.is_complete:
                logger.info(
                    "Найден незавершённый чекпойнт: %s (%d/%d). Продолжаем.",
                    cp.filename, len(cp.received), cp.total_blocks
                )
                fake = Frame(FrameType.META, cp.file_id, 0, cp.total_blocks, "")
                return fake, cp

        deadline = time.monotonic() + config.META_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            for raw in self._t.poll_new_messages():
                frame = self._try_decode_any(raw)
                if frame and frame.type == FrameType.META:
                    return frame, self._init_checkpoint(frame)
            time.sleep(config.POLL_INTERVAL)
        return None, None

    def _init_checkpoint(self, meta: Frame) -> ReceiverCheckpoint:
        data = json.loads(meta.payload)
        ckpt_path = self._ckpt_dir / f"{meta.file_id}.receiver.ckpt.json"
        cp = ReceiverCheckpoint(ckpt_path)
        cp.file_id      = meta.file_id
        cp.filename     = data["name"]
        cp.sha256       = data["sha256"]
        cp.total_blocks = data["blocks"]
        cp.file_size    = data.get("size", 0)
        cp.out_dir      = str(self._out_dir)
        cp.auto_extract = data.get("auto_extract", False)
        cp.save()
        return cp

    # ── ожидание DATA-кадра ───────────────────────────────────────────────────

    def _wait_for_data(self, file_id: str, timeout: float) -> Optional[Frame]:
        """
        Вернуть следующий DATA-кадр от отправителя.

        Сначала проверяем буфер _data_buf — poll может вернуть несколько
        блоков за раз (отправитель шлёт всё окно подряд), и мы должны
        обработать их все, а не только первый.
        """
        # Сначала — из буфера
        if self._data_buf:
            return self._data_buf.pop(0)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for raw in self._t.poll_new_messages():
                frame = self._try_decode_any(raw)
                if frame is None:
                    continue
                if frame.file_id != file_id:
                    continue
                if frame.type == FrameType.DATA:
                    # Первый кадр возвращаем сразу, остальные — в буфер
                    if not self._data_buf:
                        # Это первый — продолжаем читать остальные в буфер
                        self._data_buf.append(frame)
                    else:
                        self._data_buf.append(frame)
                elif frame.type == FrameType.ABORT:
                    logger.error("ABORT от отправителя: %s", frame.payload)
                    return None
            if self._data_buf:
                return self._data_buf.pop(0)
            time.sleep(config.POLL_INTERVAL)
        return None

    # ── хранение частичных блоков ────────────────────────────────────────────

    def _partial_block_path(self, cp: ReceiverCheckpoint, seq: int) -> Path:
        return self._ckpt_dir / f"{cp.file_id}.block.{seq:06d}.bin"

    def _save_partial_block(self, cp: ReceiverCheckpoint, seq: int, data: bytes):
        self._partial_block_path(cp, seq).write_bytes(data)

    def _restore_partial_blocks(self, cp: ReceiverCheckpoint) -> dict[int, bytes]:
        buf: dict[int, bytes] = {}
        for seq in list(cp.received):
            p = self._partial_block_path(cp, seq)
            if p.exists():
                buf[seq] = p.read_bytes()
            else:
                logger.warning("Блок seq=%d в чекпойнте, файл отсутствует — сброс.", seq)
                cp.received.discard(seq)
        return buf

    def _cleanup_partial_blocks(self, cp: ReceiverCheckpoint):
        for seq in range(cp.total_blocks):
            p = self._partial_block_path(cp, seq)
            if p.exists():
                p.unlink()

    # ── сборка файла ─────────────────────────────────────────────────────────

    def _assemble_file(
        self, cp: ReceiverCheckpoint, block_buf: dict[int, bytes]
    ) -> Path:
        out_path = self._out_dir / cp.filename
        if out_path.exists():
            out_path = self._out_dir / f"{cp.file_id}_{cp.filename}"
        with open(out_path, "wb") as f:
            for seq in range(cp.total_blocks):
                f.write(block_buf[seq])
        logger.info("Файл записан: %s (%d байт)", out_path, out_path.stat().st_size)
        return out_path

    # ── утилиты ──────────────────────────────────────────────────────────────

    def _extract_archive(self, zip_path: Path) -> Path:
        """
        Распаковать автоматически созданный ZIP-архив файла или каталога.
        После успешной распаковки архив удаляется.

        Для одиночного файла (archive.md.zip):
            → распаковывает file.md, возвращает путь к file.md
        Для каталога (my_docs.zip содержит my_docs/...):
            → распаковывает my_docs/, возвращает путь к my_docs/
        """
        extract_to = zip_path.parent
        logger.info("Распаковка архива %s в %s...", zip_path.name, extract_to)
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # Проверяем целостность архива перед распаковкой
                bad = zf.testzip()
                if bad:
                    logger.error("Архив повреждён, первый плохой файл: %s", bad)
                    return zip_path
                names = zf.namelist()
                zf.extractall(extract_to)

            zip_path.unlink()

            # Определяем что распаковалось:
            # Если в архиве один файл в корне — возвращаем его путь.
            # Если каталог — возвращаем путь к каталогу.
            top_level = {Path(n).parts[0] for n in names}
            if len(top_level) == 1:
                extracted_path = extract_to / list(top_level)[0]
            else:
                # Несколько элементов в корне — возвращаем директорию распаковки
                extracted_path = extract_to

            logger.info("Распаковано: %s", extracted_path)
            return extracted_path

        except Exception as e:
            logger.error("Ошибка распаковки архива: %s", e)
            return zip_path

    def _try_decode_any(self, raw: str) -> Optional[Frame]:
        try:
            return Frame.decode(raw)
        except FrameDecodeError as e:
            logger.warning("Битый кадр: %s", e)
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