"""
Тесты разбора снимков ленты (scripts/analyze_feed.py).

Запуск:
    python -m pytest onix_ft/tests/test_analyze_feed.py -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft.core.protocol import make_ack_frame, make_data_frame, make_file_id
from onix_ft.scripts import analyze_feed
from onix_ft.transport.selenium_driver import append_feed_records, feed_record


def _write_dump(path: Path, frames: list, start_id: int = 0):
    """Сложить снимок ленты из готовых кадров — как это делает транспорт."""
    records = [
        feed_record(i, f"el-{start_id + i}", seen=False, bubbles=1, text=text[:60])
        for i, text in enumerate(frames)
    ]
    append_feed_records(path, records)


def test_decode_head_recovers_type_and_seq():
    file_id = make_file_id()
    text = make_data_frame(file_id, seq=42, total=100, raw_block=b"\x00" * 30).encode()

    head = text[:60]                      # снимок хранит только начало текста
    frame = analyze_feed.decode_head(head)

    assert frame["type"] == "DATA"
    assert frame["seq"] == 42
    assert frame["file_id"] == file_id


def test_decode_head_reads_short_frames_too():
    file_id = make_file_id()
    head = make_ack_frame(file_id, seq=7).encode()[:60]

    frame = analyze_feed.decode_head(head)

    assert frame["type"] == "ACK"
    assert frame["seq"] == 7


def test_decode_head_ignores_regular_chat_messages():
    assert analyze_feed.decode_head("Привет, как дела?") == {}
    assert analyze_feed.decode_head("") == {}
    assert analyze_feed.decode_head("##FT сломанный") == {}


def test_read_dump_counts_each_message_once(tmp_path):
    """Одно сообщение попадает в снимок при каждом опросе — считаем один раз."""
    dump = tmp_path / "feed.jsonl"
    file_id = make_file_id()
    text = make_data_frame(file_id, seq=1, total=10, raw_block=b"x" * 10).encode()

    for _ in range(3):                    # три опроса подряд видят ту же строку
        append_feed_records(dump, [feed_record(0, "el-1", False, 1, text[:60])])

    frames = analyze_feed.read_dump(dump)

    assert len(frames) == 1
    assert frames[0]["seq"] == 1


def test_gaps_finds_missing_blocks(tmp_path):
    """Дыра в снимке = сообщение не появилось в ленте вообще."""
    dump = tmp_path / "feed.jsonl"
    file_id = make_file_id()
    texts = [
        make_data_frame(file_id, seq=s, total=10, raw_block=b"y" * 10).encode()
        for s in (0, 1, 2, 4, 5)          # блока 3 нет — как в инциденте
    ]
    _write_dump(dump, texts)

    counter = analyze_feed.data_seqs(analyze_feed.read_dump(dump))

    assert analyze_feed.gaps(counter) == [3]


def test_gaps_empty_when_everything_arrived(tmp_path):
    dump = tmp_path / "feed.jsonl"
    file_id = make_file_id()
    texts = [
        make_data_frame(file_id, seq=s, total=10, raw_block=b"z" * 10).encode()
        for s in range(5)
    ]
    _write_dump(dump, texts)

    counter = analyze_feed.data_seqs(analyze_feed.read_dump(dump))

    assert analyze_feed.gaps(counter) == []
    assert sum(counter.values()) == 5


def test_lost_in_transit_is_visible_by_comparing_sides(tmp_path):
    """
    Главный сценарий инструмента: блок есть в ленте отправителя, но нет
    у получателя — значит мессенджер не довёз опубликованное сообщение.
    """
    file_id = make_file_id()
    made = {
        s: make_data_frame(file_id, seq=s, total=10, raw_block=b"q" * 10).encode()
        for s in range(5)
    }

    sender_dump = tmp_path / "sender.jsonl"
    _write_dump(sender_dump, list(made.values()))

    receiver_dump = tmp_path / "receiver.jsonl"
    _write_dump(receiver_dump, [made[s] for s in (0, 1, 2, 4)], start_id=100)

    sent     = analyze_feed.data_seqs(analyze_feed.read_dump(sender_dump))
    received = analyze_feed.data_seqs(analyze_feed.read_dump(receiver_dump))

    assert analyze_feed.gaps(sent) == []                 # отправитель опубликовал всё
    assert sorted(set(sent) - set(received)) == [3]      # а доехало не всё


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
