"""
Чекпойнт прогресса передачи — JSON-файл рядом с передаваемым файлом.

Отправитель пишет: file_id, путь, sha256, какой блок последний подтверждён.
Получатель пишет: file_id, имя файла, какие блоки получены (bitmask в виде списка).

При перезапуске скрипты читают чекпойнт и продолжают с последнего подтверждённого места.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


# ── отправитель ──────────────────────────────────────────────────────────────

class SenderCheckpoint:
    def __init__(self, ckpt_path: Path):
        self._path = ckpt_path
        self.file_id:       str = ""
        self.source_path:   str = ""
        self.sha256:        str = ""
        self.total_blocks:  int = 0
        self.last_acked:    int = -1   # последний подтверждённый блок (-1 = META не подтверждена)
        self.meta_acked:    bool = False
        # True если передаётся автоматически созданный ZIP-архив каталога
        self.auto_extract:  bool = False

    # ── персистентность ──────────────────────────────────────────────────────

    def save(self):
        data = {
            "role":         "sender",
            "file_id":      self.file_id,
            "source_path":  self.source_path,
            "sha256":       self.sha256,
            "total_blocks": self.total_blocks,
            "last_acked":   self.last_acked,
            "meta_acked":   self.meta_acked,
            "auto_extract": self.auto_extract,
        }
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, ckpt_path: Path) -> Optional["SenderCheckpoint"]:
        if not ckpt_path.exists():
            return None
        try:
            data = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if data.get("role") != "sender":
                return None
            cp = cls(ckpt_path)
            cp.file_id      = data["file_id"]
            cp.source_path  = data["source_path"]
            cp.sha256       = data["sha256"]
            cp.total_blocks = data["total_blocks"]
            cp.last_acked   = data["last_acked"]
            cp.meta_acked   = data.get("meta_acked", False)
            cp.auto_extract = data.get("auto_extract", False)
            return cp
        except Exception:
            return None

    def delete(self):
        if self._path.exists():
            self._path.unlink()

    # ── обновление ───────────────────────────────────────────────────────────

    def confirm_meta(self):
        self.meta_acked = True
        self.save()

    def confirm_block(self, seq: int):
        if seq > self.last_acked:
            self.last_acked = seq
            self.save()

    @property
    def next_seq(self) -> int:
        """Следующий блок для отправки (после перезапуска — продолжаем)."""
        return self.last_acked + 1

    @property
    def is_complete(self) -> bool:
        return self.last_acked >= self.total_blocks - 1


# ── получатель ───────────────────────────────────────────────────────────────

class ReceiverCheckpoint:
    def __init__(self, ckpt_path: Path):
        self._path = ckpt_path
        self.file_id:      str       = ""
        self.filename:     str       = ""
        self.sha256:       str       = ""
        self.total_blocks: int       = 0
        self.file_size:    int       = 0
        self.received:     set[int]  = set()   # полученные и проверенные блоки
        self.out_dir:      str       = ""
        # True если файл является автоматически созданным ZIP-архивом каталога.
        # Получатель должен распаковать его после приёма.
        self.auto_extract: bool      = False

    # ── персистентность ──────────────────────────────────────────────────────

    def save(self):
        data = {
            "role":         "receiver",
            "file_id":      self.file_id,
            "filename":     self.filename,
            "sha256":       self.sha256,
            "total_blocks": self.total_blocks,
            "file_size":    self.file_size,
            "received":     sorted(self.received),
            "out_dir":      self.out_dir,
            "auto_extract": self.auto_extract,
        }
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, ckpt_path: Path) -> Optional["ReceiverCheckpoint"]:
        if not ckpt_path.exists():
            return None
        try:
            data = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if data.get("role") != "receiver":
                return None
            cp = cls(ckpt_path)
            cp.file_id      = data["file_id"]
            cp.filename     = data["filename"]
            cp.sha256       = data["sha256"]
            cp.total_blocks = data["total_blocks"]
            cp.file_size    = data.get("file_size", 0)
            cp.received     = set(data.get("received", []))
            cp.out_dir      = data.get("out_dir", "")
            cp.auto_extract = data.get("auto_extract", False)
            return cp
        except Exception:
            return None

    def delete(self):
        if self._path.exists():
            self._path.unlink()

    # ── обновление ───────────────────────────────────────────────────────────

    def mark_received(self, seq: int):
        self.received.add(seq)
        self.save()

    @property
    def missing_blocks(self) -> list[int]:
        return [i for i in range(self.total_blocks) if i not in self.received]

    @property
    def is_complete(self) -> bool:
        return len(self.received) >= self.total_blocks