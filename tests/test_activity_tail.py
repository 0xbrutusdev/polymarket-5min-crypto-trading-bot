import json
from pathlib import Path

from spork30.activity import Book, LogTail


def _append(path: Path, *records: dict, newline: bool = True) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for i, r in enumerate(records):
            fh.write(json.dumps(r))
            if newline or i < len(records) - 1:
                fh.write("\n")


def test_logtail_reads_only_appended_bytes(tmp_path):
    path = tmp_path / "decisions.jsonl"
    tail = LogTail(path)
    assert tail.read_new() == []
    _append(path, {"event": "a"}, {"event": "b"})
    assert [r["event"] for r in tail.read_new()] == ["a", "b"]
    assert tail.read_new() == []
    _append(path, {"event": "c"})
    assert [r["event"] for r in tail.read_new()] == ["c"]


def test_logtail_waits_for_complete_line(tmp_path):
    path = tmp_path / "decisions.jsonl"
    tail = LogTail(path)
    _append(path, {"event": "partial"}, newline=False)
    assert tail.read_new() == []
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    assert [r["event"] for r in tail.read_new()] == ["partial"]


def test_logtail_resets_on_truncate(tmp_path):
    path = tmp_path / "decisions.jsonl"
    tail = LogTail(path)
    _append(path, {"event": "a"}, {"event": "b"}, {"event": "c"})
    assert len(tail.read_new()) == 3
    path.write_text("")
    _append(path, {"event": "fresh"})
    assert [r["event"] for r in tail.read_new()] == ["fresh"]


def test_logtail_does_not_reread_whole_file(tmp_path, monkeypatch):
    """Regression for the 100 MB-per-tick re-read: bytes read per call must be
    bounded by what was appended, not by the file size."""
    path = tmp_path / "decisions.jsonl"
    _append(path, *({"event": "pad", "x": "y" * 200} for _ in range(2000)))
    tail = LogTail(path)
    tail.read_new()
    read_sizes: list[int] = []
    real_open = Path.open

    def spy_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        real_read = handle.read

        def read(n=-1):
            data = real_read(n)
            read_sizes.append(len(data))
            return data

        handle.read = read
        return handle

    monkeypatch.setattr(Path, "open", spy_open)
    _append(path, {"event": "tail"})
    assert [r["event"] for r in tail.read_new()] == ["tail"]
    assert read_sizes and max(read_sizes) < 200


def test_book_surfaces_loop_errors_on_tape():
    book = Book()
    book.apply({"event": "loop_error", "slug": "", "ts": 10, "error": "exchange 502"})
    assert book.tape[-1].event == "error"
    assert "502" in book.tape[-1].detail
    assert book.counts["loop_error"] == 1


def test_book_realized_excludes_open_cycle_split_debit():
    book = Book()
    book.apply({"event": "split", "slug": "btc-updown-5m-300", "ts": 100, "size": "750", "up": "750", "down": "750"})
    assert book.realized() == 0
    assert book.open_cash() == -750
    assert book.closed_count() == 0
