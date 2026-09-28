"""Xiaohongshu backend: thin JSON wrapper around the installed ``xhs`` CLI.

All API access (feed / hot / search / note detail / comments / login state)
is delegated to https://github.com/jackwener/xiaohongshu-cli; this module only
spawns the CLI with ``--json`` and normalizes the payloads for the TUI.
Image downloads still go through ``requests`` with a local cache.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from .config import cache_root

BASE = "https://www.xiaohongshu.com"
XHS_BIN = os.environ.get("XHS_BIN") or shutil.which("xhs") or "xhs"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Sorted by preference: default (no watermark) > preview > watermark.
_IMG_SCENE_RANK = {
    "WB_DFT": 0,
    "FD_WM_WEBP": 9,
    "FD_PRV_WEBP": 2,
    "WB_PRV": 3,
    "FD_DFT": 1,
}

_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)")
_BARE_ID_RE = re.compile(r"^[0-9a-fA-F]{16,32}$")


class XhsError(Exception):
    pass


class NotLoggedIn(XhsError):
    pass


class VerificationRequired(XhsError):
    """XHS 风控要求验证码 —— 不是登录过期。"""
    pass


# --------------------------------------------------------------------------
# xhs CLI runner
# --------------------------------------------------------------------------


def _classify_error(code: str, message: str) -> XhsError:
    """Map the CLI's stable error codes to our exception types.

    Only ``not_authenticated`` means the session is gone; captcha/风控
    (``verification_required``) and IP blocks must NOT be reported as a
    login expiry — the user is usually still logged in.
    """
    if code == "not_authenticated":
        return NotLoggedIn("登录已过期，请重新登录：xhs login")
    if code == "verification_required":
        detail = message or "需要完成验证码"
        return VerificationRequired(
            f"触发小红书安全验证（{detail}）——这不是登录过期。"
            "请稍后重试，或在浏览器打开 xiaohongshu.com 完成验证后再试"
        )
    if code == "ip_blocked":
        return XhsError("当前网络 IP 被小红书限制，请更换网络后重试")
    if code == "signature_error":
        return XhsError("请求签名失效，请更新 xhs CLI 后重试")
    if code == "unsupported_operation":
        return XhsError(f"xhs CLI 不支持该操作：{message}")
    return XhsError(message or "xhs 命令执行失败")


def _run_xhs(args: list[str], timeout: int = 60) -> dict:
    """Run ``xhs <args> --json`` and return the ``data`` payload."""
    cmd = [XHS_BIN, *args, "--json"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
    except FileNotFoundError as exc:
        raise XhsError(
            "未找到 xhs 命令，请先安装："
            "https://github.com/jackwener/xiaohongshu-cli"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise XhsError("xhs 命令超时，请稍后重试") from exc

    payload: dict | None = None
    out = (proc.stdout or "").strip()
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = None

    if proc.returncode != 0 or not (payload and payload.get("ok")):
        error = (payload or {}).get("error") or {}
        message = str(error.get("message") or "").strip()
        code = str(error.get("code") or "").strip()
        if not message:
            message = (proc.stderr or "").strip().splitlines()[-1:] or ""
            message = message[0] if message else "xhs 命令执行失败"
        raise _classify_error(code, message)
    return payload.get("data") or {}


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------


def fmt_num(n: int | None) -> str:
    n = int(n or 0)
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_time(ts: int | None) -> str:
    if not ts:
        return ""
    d = time.time() - int(ts)
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d / 60)}分钟前"
    if d < 86400:
        return f"{int(d / 3600)}小时前"
    if d < 86400 * 30:
        return f"{int(d / 86400)}天前"
    lt = time.localtime(ts)
    if lt.tm_year != time.localtime().tm_year:
        return time.strftime("%Y-%m-%d", lt)
    return time.strftime("%m-%d", lt)


def parse_count(value) -> int:
    """Parse XHS counts: ``"1406"`` / ``"2.6万"`` / ``"1.2亿"`` / ``3.4k``."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    match = re.match(r"([\d.]+)\s*(亿|万|[wWkK])?", text)
    if not match:
        return 0
    try:
        number = float(match.group(1))
    except ValueError:
        return 0
    unit = match.group(2) or ""
    if unit == "亿":
        number *= 100_000_000
    elif unit in ("万", "w", "W"):
        number *= 10_000
    elif unit in ("k", "K"):
        number *= 1_000
    return int(number)


def _ms_to_s(value) -> int:
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return ts // 1000 if ts > 10**12 else ts


def _image_url(img) -> str:
    """Pick the best URL from an XHS image / cover object."""
    if not isinstance(img, dict):
        return ""
    for key in ("url_default", "url"):
        url = str(img.get(key) or "").strip()
        if url:
            return url
    best, best_rank = "", 99
    for entry in img.get("info_list") or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        rank = _IMG_SCENE_RANK.get(str(entry.get("image_scene") or ""), 50)
        if rank < best_rank:
            best, best_rank = url, rank
    if best:
        return best
    return str(img.get("url_pre") or "").strip()


def _video_url(video) -> str:
    """Best-effort mp4 URL from an XHS ``video`` object."""
    if not isinstance(video, dict):
        return ""
    streams: list[dict] = []
    for container in (video.get("media"), video.get("media_v2")):
        if not isinstance(container, dict):
            continue
        stream = container.get("stream")
        if not isinstance(stream, dict):
            continue
        for entries in stream.values():
            if isinstance(entries, list):
                streams.extend(e for e in entries if isinstance(e, dict))
    best_url, best_size = "", -1
    for entry in streams:
        url = str(entry.get("master_url") or "").strip()
        if not url:
            continue
        size = entry.get("size") or entry.get("video_bitrate") or 0
        try:
            size_value = int(size or 0)
        except (TypeError, ValueError):
            size_value = 0
        if size_value > best_size:
            best_url, best_size = url, size_value
    return best_url


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class FeedItem:
    kind: str = "note"  # note | video
    rid: str = ""  # note id
    xsec_token: str = ""
    title: str = ""
    author: str = ""
    author_id: str = ""
    excerpt: str = ""  # desc (plain text)
    votes: int = 0  # likes
    collects: int = 0
    comments: int = 0
    created: int = 0  # seconds
    url: str = ""
    content: str = ""  # desc (plain text)
    images: list[str] = field(default_factory=list)
    thumb: str = ""  # cover image
    video_url: str = ""
    duration: int = 0  # video seconds
    tags: list[str] = field(default_factory=list)
    ip: str = ""
    rank: int = 0  # hot-list ranking (0 = not ranked)
    search_match: bool = False

    @property
    def badge(self) -> str:
        return {"video": "视频", "note": "图文"}.get(self.kind, "笔记")

    @property
    def note_url(self) -> str:
        return self.url or (
            f"{BASE}/explore/{self.rid}" if self.rid else ""
        )


@dataclass
class Comment:
    id: str = ""
    author: str = ""
    content: str = ""
    votes: int = 0
    created: int = 0
    ip: str = ""
    sub_count: int = 0
    replies: list["Comment"] = field(default_factory=list)


# --------------------------------------------------------------------------
# Payload normalization
# --------------------------------------------------------------------------


def _note_title(card: dict) -> str:
    return str(card.get("title") or card.get("display_title") or "").strip()


def _note_url(note_id: str) -> str:
    return f"{BASE}/explore/{note_id}" if note_id else ""


def _normalize_card(item: dict) -> FeedItem | None:
    """Feed / hot / search list item -> FeedItem (summary only)."""
    if not isinstance(item, dict):
        return None
    card = item.get("note_card") or {}
    if not isinstance(card, dict):
        return None
    user = card.get("user") or {}
    interact = card.get("interact_info") or {}
    note_id = str(item.get("id") or card.get("note_id") or "")
    kind = "video" if card.get("type") == "video" else "note"
    images = [
        url
        for url in (_image_url(img) for img in card.get("image_list") or [])
        if url
    ]
    video = card.get("video") or {}
    item_out = FeedItem(
        kind=kind,
        rid=note_id,
        xsec_token=str(item.get("xsec_token") or card.get("xsec_token") or ""),
        title=_note_title(card),
        author=str(user.get("nickname") or user.get("nick_name") or ""),
        author_id=str(user.get("user_id") or ""),
        excerpt=str(card.get("desc") or "").strip(),
        votes=parse_count(interact.get("liked_count")),
        collects=parse_count(interact.get("collected_count")),
        comments=parse_count(
            interact.get("comment_count") or interact.get("comment_count_total")
        ),
        created=_ms_to_s(card.get("time") or card.get("last_update_time")),
        url=_note_url(note_id),
        images=images[:30],
        thumb=_image_url(card.get("cover") or {}),
        duration=int((video.get("capa") or {}).get("duration") or 0),
        tags=[
            str(tag.get("name") or "")
            for tag in card.get("tag_list") or []
            if tag.get("name")
        ],
        ip=str(card.get("ip_location") or ""),
    )
    if not item_out.title and not item_out.thumb:
        return None
    return item_out


def _normalize_detail(data: dict) -> FeedItem:
    """``xhs read`` payload -> full FeedItem (desc + all images + video)."""
    items = data.get("items") or []
    if not items:
        raise XhsError("笔记不存在或已被删除")
    raw = items[0]
    card = raw.get("note_card") or {}
    if not isinstance(card, dict):
        raise XhsError("笔记数据异常")
    user = card.get("user") or {}
    interact = card.get("interact_info") or {}
    note_id = str(raw.get("id") or card.get("note_id") or "")
    images = [
        url
        for url in (_image_url(img) for img in card.get("image_list") or [])
        if url
    ]
    desc = str(card.get("desc") or "").strip()
    video = card.get("video") or {}
    item = FeedItem(
        kind="video" if card.get("type") == "video" else "note",
        rid=note_id,
        xsec_token=str(raw.get("xsec_token") or card.get("xsec_token") or ""),
        title=_note_title(card),
        author=str(user.get("nickname") or user.get("nick_name") or ""),
        author_id=str(user.get("user_id") or ""),
        excerpt=desc[:200],
        votes=parse_count(interact.get("liked_count")),
        collects=parse_count(interact.get("collected_count")),
        comments=parse_count(interact.get("comment_count")),
        created=_ms_to_s(
            card.get("time") or card.get("last_update_time")
        ),
        url=_note_url(note_id),
        content=desc,
        images=images[:30] or ([_image_url(card.get("cover") or {})] if card.get("cover") else []),
        thumb=_image_url(card.get("cover") or {}) or (images[0] if images else ""),
        video_url=_video_url(video),
        duration=int((video.get("capa") or {}).get("duration") or 0),
        tags=[
            str(tag.get("name") or "")
            for tag in card.get("tag_list") or []
            if tag.get("name")
        ],
        ip=str(card.get("ip_location") or ""),
    )
    return item


def _normalize_comment(raw: dict) -> Comment:
    user = raw.get("user_info") or {}
    replies = [
        _normalize_comment(sub)
        for sub in (raw.get("sub_comments") or [])
        if isinstance(sub, dict)
    ]
    return Comment(
        id=str(raw.get("id") or ""),
        author=str(user.get("nickname") or ""),
        content=str(raw.get("content") or "").strip(),
        votes=parse_count(raw.get("like_count")),
        created=_ms_to_s(raw.get("create_time")),
        ip=str(raw.get("ip_location") or ""),
        sub_count=parse_count(raw.get("sub_comment_count")),
        replies=replies,
    )


# --------------------------------------------------------------------------
# Image cache
# --------------------------------------------------------------------------


def _image_cache_dir() -> Path:
    return cache_root() / "img"


def _image_cache_key(url: str) -> Path:
    return _image_cache_dir() / hashlib.md5(url.encode()).hexdigest()


def _image_cache_get(url: str) -> bytes | None:
    path = _image_cache_key(url)
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        data = path.read_bytes()
        from io import BytesIO

        from PIL import Image

        Image.open(BytesIO(data)).verify()
        return data
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _image_cache_put(url: str, data: bytes) -> None:
    try:
        _image_cache_dir().mkdir(parents=True, exist_ok=True)
        _image_cache_key(url).write_bytes(data)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class XhsClient:
    """Talks to Xiaohongshu via the ``xhs`` CLI (JSON mode)."""

    _NOTE_CACHE_MAX = 300

    def __init__(self):
        self._feed_seen: set[str] = set()
        self._search_key = ""
        self._search_seen: set[str] = set()
        # In-memory note cache: opening a note right after its count/cover
        # fetch (or re-opening it) does not hit the API again.
        self._note_cache: dict[str, FeedItem] = {}

    # -- auth -------------------------------------------------------------

    def status(self) -> dict | None:
        """Return the logged-in user dict, or None when unauthenticated."""
        try:
            data = _run_xhs(["status"], timeout=30)
        except NotLoggedIn:
            return None
        if not data.get("authenticated"):
            return None
        user = data.get("user") or {}
        return user if user else None

    def me(self) -> dict | None:
        return self.status()

    def is_logged_in(self) -> bool:
        return self.status() is not None

    # -- feeds ------------------------------------------------------------

    def feed(self) -> tuple[list[FeedItem], bool]:
        """Recommendation feed; the CLI has no cursor, so we de-duplicate."""
        data = _run_xhs(["feed"], timeout=90)
        items: list[FeedItem] = []
        fresh = False
        for raw in data.get("items") or []:
            item = _normalize_card(raw)
            if item is None:
                continue
            if item.rid and item.rid in self._feed_seen:
                continue
            if item.rid:
                self._feed_seen.add(item.rid)
            items.append(item)
            fresh = True
        return items, not fresh

    def hot(self, category: str = "") -> tuple[list[FeedItem], bool]:
        """Hot notes, optionally filtered by category (single page)."""
        args = ["hot"]
        if category:
            args += ["-c", category]
        data = _run_xhs(args, timeout=60)
        items: list[FeedItem] = []
        for index, raw in enumerate(data.get("items") or [], 1):
            item = _normalize_card(raw)
            if item is None:
                continue
            item.rank = index
            items.append(item)
        return items, True

    # -- search -----------------------------------------------------------

    def search(self, query: str, page: int = 1) -> tuple[list[FeedItem], bool]:
        if query != self._search_key or page <= 1:
            self._search_seen.clear()
        self._search_key = query
        data = _run_xhs(["search", query, "--page", str(page)], timeout=60)
        items: list[FeedItem] = []
        for raw in data.get("items") or []:
            item = _normalize_card(raw)
            if item is None:
                continue
            if item.rid and item.rid in self._search_seen:
                continue
            if item.rid:
                self._search_seen.add(item.rid)
            item.search_match = True
            items.append(item)
        return items, not bool(data.get("has_more"))

    # -- note detail ------------------------------------------------------

    def read(self, note_id: str, xsec_token: str = "") -> FeedItem:
        cached = self._note_cache.get(note_id)
        if cached is not None:
            return cached
        args = ["read", note_id]
        if xsec_token:
            args += ["--xsec-token", xsec_token]
        data = _run_xhs(args, timeout=60)
        item = _normalize_detail(data)
        if len(self._note_cache) >= self._NOTE_CACHE_MAX:
            self._note_cache.pop(next(iter(self._note_cache)))
        self._note_cache[note_id] = item
        return item

    def fetch_url(self, url: str) -> FeedItem:
        """Resolve an XHS URL (or bare note id) into a full note."""
        url = (url or "").strip()
        if not url:
            raise XhsError("请输入小红书链接")
        if "xhslink.com" in url:
            url = self._resolve_short_link(url)
        match = _NOTE_ID_RE.search(url)
        note_id = match.group(1) if match else ""
        token = ""
        try:
            token = (parse_qs(urlparse(url).query).get("xsec_token") or [""])[0]
        except Exception:
            token = ""
        if not note_id and _BARE_ID_RE.match(url):
            note_id = url
        if not note_id:
            raise XhsError(
                "无法识别的小红书链接（支持 explore / discovery/item 链接或笔记 ID）"
            )
        return self.read(note_id, token)

    @staticmethod
    def _resolve_short_link(url: str) -> str:
        try:
            resp = requests.get(
                url,
                timeout=15,
                allow_redirects=True,
                headers={"User-Agent": UA, "Referer": f"{BASE}/"},
            )
            return resp.url or url
        except Exception as exc:
            raise XhsError(f"短链解析失败：{exc}") from exc

    # -- comments ---------------------------------------------------------

    def note_comments(
        self, note_id: str, cursor: str = "", xsec_token: str = ""
    ) -> tuple[list[Comment], str, bool]:
        args = ["comments", note_id]
        if xsec_token:
            args += ["--xsec-token", xsec_token]
        if cursor:
            args += ["--cursor", cursor]
        data = _run_xhs(args, timeout=60)
        comments = [
            _normalize_comment(raw)
            for raw in data.get("comments") or []
            if isinstance(raw, dict)
        ]
        comments = [c for c in comments if c.content]
        return comments, str(data.get("cursor") or ""), bool(data.get("has_more"))

    # -- images -----------------------------------------------------------

    def fetch_image_bytes(self, url: str, timeout: int = 30) -> bytes:
        data = _image_cache_get(url)
        if data is not None:
            return data
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": UA,
                "Referer": f"{BASE}/",
                "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
            },
        )
        resp.raise_for_status()
        data = resp.content
        if not data:
            raise XhsError("图片下载为空")
        _image_cache_put(url, data)
        return data


# --------------------------------------------------------------------------
# Login flow (console, before the TUI starts)
# --------------------------------------------------------------------------


def run_login(use_qrcode: bool = False) -> bool:
    """Run ``xhs login`` interactively (browser cookies, or ``--qrcode``)."""
    args = [XHS_BIN, "login"] + (["--qrcode"] if use_qrcode else [])
    try:
        return subprocess.run(args, check=False).returncode == 0
    except FileNotFoundError:
        return False


def login_flow(client: XhsClient) -> dict | None:
    """Interactive login: browser cookies first, QR code as fallback."""
    print("=== 小红书登录 ===")
    print("[1/2] 从浏览器读取 Cookie（xhs login）…")
    if run_login():
        user = client.status()
        if user:
            print(f"  登录成功：{user.get('name', '')}")
            return user
    print("[2/2] 改用扫码登录（xhs login --qrcode）…")
    print("  请使用小红书 App 扫描终端中的二维码。")
    if run_login(use_qrcode=True):
        user = client.status()
        if user:
            print(f"  登录成功：{user.get('name', '')}")
            return user
    print("  ✗ 登录失败，请手动运行：xhs login")
    return None
