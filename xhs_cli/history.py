"""Small, privacy-friendly local viewing history (grouped by author)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config


def path() -> Path:
    if config.get("history_persistent", False):
        return config.CONFIG_DIR / "history.json"
    return config.cache_root() / "history.json"


def load() -> list[dict]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def record(item) -> None:
    """Remember a viewed note; entries are grouped by author."""
    if not config.get("history_enabled", True):
        return
    if item.kind not in {"note", "video"}:
        return
    records = load()
    author = item.author or "未知作者"
    entry = next((e for e in records if e.get("author") == author), None)
    if entry is None:
        entry = {"author": author, "viewed": 0, "notes": []}
        records.append(entry)
    entry.update(viewed=int(time.time()))
    notes = entry.setdefault("notes", [])
    nid = str(item.rid or item.note_url or item.title)
    notes[:] = [note for note in notes if note.get("nid") != nid]
    notes.insert(
        0,
        {
            "nid": nid,
            "xsec_token": item.xsec_token,
            "title": item.title or "（无标题笔记）",
            "excerpt": " ".join((item.excerpt or item.content or "").split())[:180],
            "url": item.url,
            "votes": int(item.votes or 0),
            "collects": int(item.collects or 0),
            "comments": int(item.comments or 0),
            "images": len(item.images or []),
            "kind": item.kind,
            "created": int(item.created or 0),
        },
    )
    entry["notes"] = notes[:20]
    records.sort(key=lambda e: e.get("viewed", 0), reverse=True)
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(records[:100], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def clear() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass
