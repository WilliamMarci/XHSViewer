"""Small, privacy-friendly local viewing history."""
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
    if not config.get("history_enabled", True) or item.kind not in {"answer", "question"}:
        return
    records = load()
    qid = str(item.qid or item.title or item.url)
    record = next((entry for entry in records if entry.get("qid") == qid), None)
    if record is None:
        record = {"qid": qid, "title": item.title, "url": item.url, "viewed": 0, "answers": []}
        records.append(record)
    record.update(title=item.title or record.get("title", ""), url=item.url or record.get("url", ""), viewed=int(time.time()))
    answers = record.setdefault("answers", [])
    if item.kind == "question":
        records.sort(key=lambda entry: entry.get("viewed", 0), reverse=True)
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(records[:100], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return
    aid = str(item.aid or item.url)
    answers[:] = [answer for answer in answers if answer.get("aid") != aid]
    answers.insert(0, {
        "aid": aid,
        "author": item.author or "匿名用户",
        "excerpt": " ".join((item.excerpt or "").split())[:180],
        "url": item.url,
        "votes": int(item.votes or 0),
        "comments": int(item.comments or 0),
        "images": len(item.images or []),
        "created": int(item.created or 0),
    })
    record["answers"] = answers[:10]
    records.sort(key=lambda entry: entry.get("viewed", 0), reverse=True)
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(records[:100], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clear() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass
