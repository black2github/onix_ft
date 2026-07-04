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

FRAME_PREFIX = "##FT"
FRAME_SUFFIX = "##"
PROTOCOL_VERSION = "v1"

# CHUNK_BYTES и MESSAGE_MAX_LEN вынесены в config.py.
# Значения подтягиваются при первом обращении к encode()/split_file().
def _get_chunk_bytes() -> int:
    try:
        from .. import config
        return config.CHUNK_BYTES
    except Exception:
        return 3027

def _get_message_max_len() -> int:
    try:
        from .. import config
        return config.MESSAGE_MAX_LEN
    except Exception:
        return 4095

# Для обратной совместимости и использования в split_file()
CHUNK_BYTES = _get_chunk_bytes()
MESSAGE_MAX_LEN = _get_message_max_len()


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
        """
        Превратить кадр в строку для отправки в чат.

        Стратегия кодирования зависит от типа кадра:

        DATA — payload уже является base64-строкой бинарных данных, поэтому
        кодируем только заголовок, payload оставляем как есть:
            ##FT|<base64(v1|DATA|file_id|seq|total|crc)>|<base64_payload>##
        Двойное base64-кодирование payload увеличило бы размер на ~33%
        и потребовало бы уменьшения CHUNK_BYTES на 25% — неприемлемая потеря.

        Все остальные типы (META, ACK, NACK, DONE, ABORT) — payload небольшой,
        кодируем весь inner целиком:
            ##FT|<base64(v1|TYPE|file_id|seq|total|payload|crc)>##
        Имя файла, SHA256, размер и прочие метаданные полностью скрыты.
        """
        body_for_crc = f"{self.type.value}|{self.file_id}|{self.seq}|{self.total}|{self.payload}"
        crc = _crc32hex(body_for_crc)

        # Получаем ключ шифрования из config (если задан)
        from .. import config as _cfg
        key = _cfg.FRAME_KEY.encode("utf-8") if getattr(_cfg, "FRAME_KEY", "") else b""

        if self.type == FrameType.DATA:
            # Заголовок: XOR → base64. Payload (уже base64) — как есть.
            header_plain = f"{PROTOCOL_VERSION}|{self.type.value}|{self.file_id}|{self.seq}|{self.total}|{crc}"
            header_enc   = _xor_cipher(header_plain.encode("utf-8"), key)
            header_b64   = base64.b64encode(header_enc).decode("ascii")
            text = f"{FRAME_PREFIX}{header_b64}|{self.payload}{FRAME_SUFFIX}"
        else:
            # Весь inner: XOR → base64 — имя файла, SHA256 и прочее скрыто.
            inner_plain = f"{PROTOCOL_VERSION}|{self.type.value}|{self.file_id}|{self.seq}|{self.total}|{self.payload}|{crc}"
            inner_enc   = _xor_cipher(inner_plain.encode("utf-8"), key)
            inner_b64   = base64.b64encode(inner_enc).decode("ascii")
            text = f"{FRAME_PREFIX}{inner_b64}{FRAME_SUFFIX}"

        max_len = _get_message_max_len()
        if len(text) > max_len:
            raise ValueError(
                f"Кадр {self.type} seq={self.seq} превышает лимит сообщения: "
                f"{len(text)} > {max_len}"
            )
        return text

    # ── десериализация ────────────────────────────────────────────────────────

    @staticmethod
    def decode(text: str) -> Optional["Frame"]:
        """
        Распарсить строку сообщения чата.
        Возвращает Frame или None, если строка — не наш кадр.
        Бросает FrameDecodeError при битом кадре.

        Два формата в зависимости от типа кадра:

        DATA:  ##FT|<base64(v1|DATA|file_id|seq|total|crc)>|<payload>##
               Содержит "|" после первого base64-сегмента.

        Прочие: ##FT|<base64(v1|TYPE|file_id|seq|total|payload|crc)>##
               Весь inner — один base64-блок без дополнительного "|".

        Различаем форматы: пробуем декодировать весь inner как base64.
        Если успешно и внутри 7 полей — это не-DATA формат.
        Если нет (содержит "|") — это DATA формат.
        """
        text = text.strip()
        if not (text.startswith(FRAME_PREFIX) and text.endswith(FRAME_SUFFIX)):
            return None  # обычное сообщение чата — игнорируем

        inner = text[len(FRAME_PREFIX):-len(FRAME_SUFFIX)]

        # Получаем ключ шифрования из config (если задан)
        from .. import config as _cfg
        key = _cfg.FRAME_KEY.encode("utf-8") if getattr(_cfg, "FRAME_KEY", "") else b""

        # Пробуем декодировать весь inner как base64 (формат не-DATA)
        try:
            inner_dec   = base64.b64decode(inner.encode("ascii"))
            inner_plain = _xor_cipher(inner_dec, key).decode("utf-8")
            parts = inner_plain.split("|")
            # Формат: v1|TYPE|file_id|seq|total|payload|crc — минимум 7 полей
            # payload сам может содержать "|" (JSON META)
            if len(parts) >= 7:
                version   = parts[0]
                ftype_str = parts[1]
                file_id   = parts[2]
                seq_str   = parts[3]
                total_str = parts[4]
                crc_received = parts[-1]
                payload   = "|".join(parts[5:-1])

                if version != PROTOCOL_VERSION:
                    raise FrameDecodeError(
                        f"Неподдерживаемая версия: {version!r}"
                    )
                body_for_crc = f"{ftype_str}|{file_id}|{seq_str}|{total_str}|{payload}"
                crc_expected = _crc32hex(body_for_crc)
                if crc_received != crc_expected:
                    raise FrameDecodeError(
                        f"CRC mismatch: ожидали {crc_expected}, получили {crc_received}"
                    )
                try:
                    ftype = FrameType(ftype_str)
                except ValueError:
                    raise FrameDecodeError(f"Неизвестный тип: {ftype_str!r}")
                try:
                    seq   = int(seq_str)
                    total = int(total_str)
                except ValueError:
                    raise FrameDecodeError(f"Нечисловые seq/total")
                return Frame(type=ftype, file_id=file_id, seq=seq, total=total, payload=payload)
        except FrameDecodeError:
            raise
        except Exception:
            pass  # не base64 или меньше 7 полей — пробуем DATA-формат

        # DATA-формат: ##FT|<base64(заголовок)>|<payload>##
        sep = inner.find("|")
        if sep == -1:
            raise FrameDecodeError(f"Не удалось распознать формат кадра: {inner[:40]!r}")

        header_b64 = inner[:sep]
        payload    = inner[sep + 1:]

        try:
            header_dec   = base64.b64decode(header_b64.encode("ascii"))
            header_plain = _xor_cipher(header_dec, key).decode("utf-8")
        except Exception:
            raise FrameDecodeError(f"Ошибка декодирования заголовка DATA: {header_b64[:20]!r}")

        hparts = header_plain.split("|")
        if len(hparts) != 6:
            raise FrameDecodeError(f"Неверная структура заголовка DATA: {header_plain!r}")

        version, ftype_str, file_id, seq_str, total_str, crc_received = hparts

        if version != PROTOCOL_VERSION:
            raise FrameDecodeError(f"Неподдерживаемая версия: {version!r}")

        body_for_crc = f"{ftype_str}|{file_id}|{seq_str}|{total_str}|{payload}"
        crc_expected = _crc32hex(body_for_crc)
        if crc_received != crc_expected:
            raise FrameDecodeError(
                f"CRC mismatch: ожидали {crc_expected}, получили {crc_received}"
            )

        try:
            ftype = FrameType(ftype_str)
        except ValueError:
            raise FrameDecodeError(f"Неизвестный тип: {ftype_str!r}")

        try:
            seq   = int(seq_str)
            total = int(total_str)
        except ValueError:
            raise FrameDecodeError(f"Нечисловые seq/total")

        return Frame(type=ftype, file_id=file_id, seq=seq, total=total, payload=payload)


class FrameDecodeError(Exception):
    """Кадр с нашими маркерами, но содержимое повреждено."""
    pass


# ── хелперы ──────────────────────────────────────────────────────────────────

def _crc32hex(s: str) -> str:
    """CRC32 строки в UTF-8, результат — 8-символьный lowercase hex."""
    return format(zlib.crc32(s.encode("utf-8")) & 0xFFFFFFFF, "08x")


def _xor_cipher(data: bytes, key: bytes) -> bytes:
    """
    XOR-шифрование с повторяющимся ключом.
    Применяется к заголовку кадра перед base64-кодированием.
    Без знания ключа декодированный base64 даёт бессмысленный набор байт.
    """
    if not key:
        return data
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


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
    """Разбить файл на блоки по CHUNK_BYTES байт (из config.py)."""
    chunk_size = _get_chunk_bytes()
    blocks = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
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

def make_meta_frame(
    file_id:      str,
    path:         Path,
    total_blocks: int,
    sha256:       str,
    auto_extract: bool = False,
) -> Frame:
    """
    Создать META-кадр.

    auto_extract=True означает что файл является временным ZIP-архивом,
    созданным автоматически из каталога на стороне отправителя.
    Получатель должен распаковать его и удалить архив после распаковки.
    Для обычных файлов (включая ZIP отправленные вручную) — False.
    """
    meta = {
        "name":   path.name,
        "size":   path.stat().st_size,
        "sha256": sha256,
        "blocks": total_blocks,
    }
    if auto_extract:
        meta["auto_extract"] = True
    payload = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
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