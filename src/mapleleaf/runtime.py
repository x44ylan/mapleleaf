"""Exclusive ownership and append-only local run evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid


class WindowLease:
    """OS-owned lock: released automatically if the owning process exits."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or Path(__file__).resolve().parents[2] / "data" / "window.lock")
        self.file = None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("Another Mapleleaf session owns the game. Pause its observer before a live step.") from exc
        return self

    def __exit__(self, *_):
        if self.file:
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


def new_run(root: Path, prefix="observe") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / "runs" / f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"
    target.mkdir(parents=True)
    return target


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def event(run_dir: Path, kind: str, **fields):
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": kind, **fields}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
