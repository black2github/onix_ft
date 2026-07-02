"""
OnixFT — протокол передачи файлов поверх текстового чата.

Формат кадра (одна строка, всё — ASCII):
    ##FT|v1|<type>|<file_id>|<seq>|<total>|<payload>|<crc32>##

Поля:
    type    — тип кадра: META / DATA / ACK / NACK / DONE / ABORT
    file_id — короткий идентификатор сессии передачи (8 hex-символов)
    seq     — порядковый номер блока (0-based); для META = 0
    total   — общее число DATA-блоков; для ACK/NACK = 0
    payload — зависит от типа (см. ниже)
    crc32   — crc32 по строке "type|file_id|seq|total|payload" в hex (8 символов)

Payload по типу:
    META  — JSON: {"name": <filename>, "size": <bytes>, "sha256": <hex>, "blocks": <n>}
    DATA  — base64-фрагмент исходного файла (не более CHUNK_B64_LEN символов)
    ACK   — номер подтверждённого блока (decimal string)
    NACK  — номер блока для повторной отправки (decimal string)
    DONE  — sha256 собранного файла (hex) — отправляется получателем для финальной сверки
    ABORT — текстовое сообщение об ошибке

Любая строка сообщения, не начинающаяся с ##FT|v1| и не заканчивающаяся на ##,
считается обычным сообщением чата и игнорируется.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# ── константы ────────────────────────────────────────────────────────────────

FRAME_PREFIX = "##FT|v1|"
FRAME_SUFFIX = "##"
PROTOCOL_VERSION = "v1"

# Размер одного DATA-блока в байтах исходных данных.
# base64 раздует его примерно в 4/3, то есть ~2700 байт → ~3600 символов b64.
# Оставляем запас на заголовок кадра (~120 символов) из лимита 4000 символов сообщения.
CHUNK_BYTES = 2700

# Лимит символов в одном сообщении Onix (с небольшим запасом).
MESSAGE_MAX_LEN = 4000


class FrameType(str, Enum):
    META  = "META"
    DATA  = "DATA"
    ACK   = "ACK"
    NACK  = "NACK"
    DONE  = "DONE"
    ABORT = "ABORT"


# ── датакласс кадра ──────────────────────────────────────────────────────────

@dataclass
class Frame:
    type:    FrameType
    file_id: str           # 8 hex-символов
    seq:     int           # номер блока (DATA) или подтверждаемый номер (ACK/NACK)
    total:   int           # число блоков; 0 для ACK/NACK/DONE/ABORT
    payload: str           # содержимое (см. формат выше)

    # ── сериализация ─────────────────────────────────────────────────────────

    def encode(self) -> str:
        """Превратить кадр в строку для отправки в чат."""
        body = f"{self.type.value}|{self.file_id}|{self.seq}|{self.total}|{self.payload}"
        crc  = _crc32hex(body)
        text = f"{FRAME_PREFIX}{body}|{crc}{FRAME_SUFFIX}"
        if len(text) > MESSAGE_MAX_LEN:
            raise ValueError(
                f"Кадр {self.type} seq={self.seq} превышает лимит сообщения: "
                f"{len(text)} > {MESSAGE_MAX_LEN}"
            )
        return text

    # ── десериализация ────────────────────────────────────────────────────────

    @staticmethod
    def decode(text: str) -> Optional["Frame"]:
        """
        Распарсить строку сообщения чата.
        Возвращает Frame или None, если строка — не наш кадр.
        Бросает FrameDecodeError при битом кадре (маркеры есть, но внутри ошибка).
        """
        text = text.strip()
        if not (text.startswith(FRAME_PREFIX) and text.endswith(FRAME_SUFFIX)):
            return None  # обычное сообщение чата — просто игнорируем

        inner = text[len(FRAME_PREFIX):-len(FRAME_SUFFIX)]
        parts = inner.split("|")

        # Минимум 6 полей: type, file_id, seq, total, payload, crc
        # payload сам может содержать "|" (JSON), поэтому берём последние 5 + всё остальное как payload
        if len(parts) < 6:
            raise FrameDecodeError(f"Слишком мало полей в кадре: {inner!r}")

        # CRC всегда последний элемент (8 hex-символов)
        crc_received = parts[-1]
        # payload — всё между (type|file_id|seq|total| и |crc)
        # то есть parts[0..3] + "|".join(parts[4:-1])
        ftype_str, file_id, seq_str, total_str = parts[0], parts[1], parts[2], parts[3]
        payload = "|".join(parts[4:-1])

        body_for_crc = f"{ftype_str}|{file_id}|{seq_str}|{total_str}|{payload}"
        crc_expected = _crc32hex(body_for_crc)

        if crc_received != crc_expected:
            raise FrameDecodeError(
                f"CRC mismatch: ожидали {crc_expected}, получили {crc_received}"
            )

        try:
            ftype = FrameType(ftype_str)
        except ValueError:
            raise FrameDecodeError(f"Неизвестный тип кадра: {ftype_str!r}")

        try:
            seq   = int(seq_str)
            total = int(total_str)
        except ValueError:
            raise FrameDecodeError(f"Нечисловые seq/total: {seq_str!r}/{total_str!r}")

        return Frame(type=ftype, file_id=file_id, seq=seq, total=total, payload=payload)


class FrameDecodeError(Exception):
    """Кадр с нашими маркерами, но содержимое повреждено."""
    pass


# ── хелперы ──────────────────────────────────────────────────────────────────

def _crc32hex(s: str) -> str:
    """CRC32 строки в UTF-8, результат — 8-символьный lowercase hex."""
    return format(zlib.crc32(s.encode("utf-8")) & 0xFFFFFFFF, "08x")


def make_file_id() -> str:
    """Генерация уникального file_id для новой сессии передачи."""
    import os
    return os.urandom(4).hex()


# ── утилиты для работы с файлом ──────────────────────────────────────────────

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def split_file(path: Path) -> list[bytes]:
    """Разбить файл на блоки по CHUNK_BYTES байт."""
    blocks = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_BYTES)
            if not chunk:
                break
            blocks.append(chunk)
    return blocks


def encode_data_payload(raw_block: bytes) -> str:
    """Бинарный блок → base64-строка (без переносов строк)."""
    return base64.b64encode(raw_block).decode("ascii")


def decode_data_payload(b64_str: str) -> bytes:
    """base64-строка → бинарный блок."""
    return base64.b64decode(b64_str)


# ── фабрики кадров ───────────────────────────────────────────────────────────

def make_meta_frame(file_id: str, path: Path, total_blocks: int, sha256: str) -> Frame:
    payload = json.dumps({
        "name":   path.name,
        "size":   path.stat().st_size,
        "sha256": sha256,
        "blocks": total_blocks,
    }, ensure_ascii=False, separators=(",", ":"))
    return Frame(FrameType.META, file_id, seq=0, total=total_blocks, payload=payload)


def make_data_frame(file_id: str, seq: int, total: int, raw_block: bytes) -> Frame:
    return Frame(FrameType.DATA, file_id, seq=seq, total=total,
                 payload=encode_data_payload(raw_block))


def make_ack_frame(file_id: str, seq: int) -> Frame:
    return Frame(FrameType.ACK, file_id, seq=seq, total=0, payload=str(seq))


def make_nack_frame(file_id: str, seq: int) -> Frame:
    return Frame(FrameType.NACK, file_id, seq=seq, total=0, payload=str(seq))


def make_done_frame(file_id: str, sha256: str) -> Frame:
    return Frame(FrameType.DONE, file_id, seq=0, total=0, payload=sha256)


def make_abort_frame(file_id: str, reason: str) -> Frame:
    return Frame(FrameType.ABORT, file_id, seq=0, total=0, payload=reason[:200])
