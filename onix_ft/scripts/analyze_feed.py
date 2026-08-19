"""
Разбор снимков ленты (FEED_DUMP_PATH): где теряются сообщения.

Снимок пишется транспортом, когда в config.py задан FEED_DUMP_PATH.
В нём — что видел в ленте браузер: строки, пузыри, начало текста. Начало
текста содержит закрытый заголовок кадра, поэтому по снимку восстанавливается
тип кадра и номер блока — этого хватает, чтобы понять, где пропало сообщение.

Использование:
    # только получатель — что до него доехало
    python onix_ft/scripts/analyze_feed.py receiver.jsonl

    # обе стороны — где именно потерялось
    python onix_ft/scripts/analyze_feed.py --sender sender.jsonl --receiver receiver.jsonl

Вывод отвечает на один вопрос: сообщение не было опубликовано отправителем
или было опубликовано, но не доехало до получателя.
"""

import argparse
import base64
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft import config


def decode_head(head: str) -> dict:
    """
    Восстановить заголовок кадра из поля `head` снимка ленты.

    `head` обрезан 60 символами, но закрытый заголовок короче и укладывается
    целиком — этого достаточно для типа кадра и номера блока. Возвращает
    пустой словарь, если строка — не наш кадр (обычное сообщение чата).
    """
    if not head or not head.startswith("##FT"):
        return {}
    segment = head[4:].split("|")[0].rstrip("#")
    if not segment:
        return {}
    key = (config.FRAME_KEY or "").encode("utf-8")
    try:
        raw = base64.b64decode(segment.encode("ascii"))
    except Exception:
        return {}
    if key:
        raw = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    parts = raw.decode("utf-8", "replace").split("|")
    if len(parts) < 4:
        return {}
    try:
        seq = int(parts[3])
    except ValueError:
        return {}
    return {"type": parts[1], "file_id": parts[2], "seq": seq}


def read_dump(path: Path) -> list:
    """Снимок → список расшифрованных кадров (по одному на сообщение ленты)."""
    frames = []
    seen_ids = set()
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("id") in seen_ids:
            continue           # одно сообщение попадает в снимок много раз
        seen_ids.add(record.get("id"))
        frame = decode_head(record.get("head", ""))
        if frame:
            frames.append(frame)
    return frames


def data_seqs(frames: list, file_id: str = None) -> Counter:
    """Сколько раз каждый DATA-блок появлялся в ленте."""
    return Counter(
        f["seq"] for f in frames
        if f["type"] == "DATA" and (file_id is None or f["file_id"] == file_id)
    )


def gaps(counter: Counter) -> list:
    """Номера блоков, отсутствующие внутри принятого диапазона."""
    if not counter:
        return []
    return [s for s in range(min(counter), max(counter) + 1) if s not in counter]


def describe(name: str, frames: list, file_id: str) -> Counter:
    counter = data_seqs(frames, file_id)
    acks  = sum(1 for f in frames if f["type"] == "ACK"  and f["file_id"] == file_id)
    nacks = sum(1 for f in frames if f["type"] == "NACK" and f["file_id"] == file_id)
    print(f"{name}:")
    if not counter:
        print("   DATA-сообщений нет")
        return counter
    repeats = sorted(s for s, n in counter.items() if n > 1)
    print(f"   DATA: {sum(counter.values())} сообщений, "
          f"уникальных блоков {len(counter)} (диапазон {min(counter)}..{max(counter)})")
    print(f"   дыры внутри диапазона: {gaps(counter) or 'нет'}")
    print(f"   пересылались повторно: {repeats or 'нет'}")
    print(f"   ACK: {acks}, NACK: {nacks}")
    return counter


def main():
    parser = argparse.ArgumentParser(description="Разбор снимков ленты OnixFT")
    parser.add_argument("dump", nargs="?", help="снимок одной стороны")
    parser.add_argument("--sender",   help="снимок отправителя")
    parser.add_argument("--receiver", help="снимок получателя")
    parser.add_argument("--file-id",  help="разбирать только эту сессию передачи")
    args = parser.parse_args()

    # Роль важна для вывода: один и тот же снимок читается по-разному
    # в зависимости от того, чья это лента.
    sender_path   = args.sender
    receiver_path = args.receiver
    single_path   = args.dump if not (sender_path or receiver_path) else None

    sender_frames   = read_dump(Path(sender_path))   if sender_path   else []
    receiver_frames = read_dump(Path(receiver_path)) if receiver_path else []
    single_frames   = read_dump(Path(single_path))   if single_path   else []
    all_frames = sender_frames + receiver_frames + single_frames

    if not all_frames:
        print("В снимках нет кадров OnixFT — проверьте путь и FRAME_KEY.")
        return 1

    # Все сессии, где есть блоки данных: снимок часто копится за несколько
    # прогонов, и прерванные попытки интересны не меньше последней.
    sessions = [args.file_id] if args.file_id else list(dict.fromkeys(
        f["file_id"] for f in all_frames if f["type"] == "DATA"
    ))

    for file_id in sessions:
        print(f"\n=== сессия передачи {file_id} ===")
        if single_frames:
            describe("Лента (роль не указана — задайте --sender/--receiver)",
                     single_frames, file_id)
        sent     = describe("Лента ОТПРАВИТЕЛЯ", sender_frames, file_id) if sender_frames else None
        received = describe("Лента ПОЛУЧАТЕЛЯ", receiver_frames, file_id) if receiver_frames else None

        if sent is not None and received is not None:
            not_published = gaps(sent)
            lost_in_transit = sorted(set(sent) - set(received))
            print("\nВЫВОД:")
            if not_published:
                print(f"   НЕ ОПУБЛИКОВАНЫ отправителем (нет в его же ленте): {not_published}")
                print("   → мессенджер не принял отправку (похоже на ограничение частоты)")
            if lost_in_transit:
                print(f"   опубликованы, но НЕ ДОЕХАЛИ до получателя: {lost_in_transit}")
                print("   → теряет доставку сам мессенджер")
            if not not_published and not lost_in_transit:
                print("   потерь между сторонами не видно")
    return 0


if __name__ == "__main__":
    sys.exit(main())
