"""Atomic text-file writes.

A reader (notably the in-progress POI lock scan, which reads sibling
``selection_summary.json`` / ``RUN_RECEIPT.json`` while other processes write
them) must never observe a half-written file. A direct overwrite
(``Path.write_text``) exposes exactly that window: a concurrent reader can read
a truncated file mid-write, fail to parse it, and silently drop that sibling's
claim for the round — risking a POI collision.

``atomic_write_text`` closes the window by writing to a temp file in the same
directory, fsync-ing it, then ``os.replace``-ing it into place (atomic on POSIX
same-filesystem). Ported verbatim in spirit from music_remix's
``write_receipt_atomic`` (AIGC ``video_paradigms/music_remix/receipt.py``),
which PGC's lock was copied from — the original was atomic; the port missed it.
"""

from __future__ import annotations

import os
from pathlib import Path


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so the rename is durable."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Write ``text`` to ``path`` atomically (temp file + fsync + os.replace).

    A concurrent reader sees either the previous complete file or the new
    complete file, never a partial one. On any failure the temp file is removed
    and the real target is left untouched.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return target
