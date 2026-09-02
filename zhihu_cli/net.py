"""Zhihu API client, feed item normalization, and login flows."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
import sqlite3
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests

from .config import cache_root

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE = "https://www.zhihu.com"
FEED_URL = f"{BASE}/api/v3/feed/topstory/recommend"
ME_URL = f"{BASE}/api/v4/me"

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "zhihu-viewer"
COOKIE_FILE = CONFIG_DIR / "cookies.json"
LEGACY_COOKIE_FILE = Path("cookies.txt")  # old netscape format, kept for compat

_IMG_RE = re.compile(r"<img[^>]+>", re.I)


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


class ZhihuError(Exception):
    pass


class NotLoggedIn(ZhihuError):
    pass


@dataclass
class FeedItem:
    kind: str  # answer | article | zvideo | question | pin
    rid: str = ""  # raw feed item id, used for pagination
    title: str = ""
    author: str = ""
    excerpt: str = ""
    votes: int = 0
    comments: int = 0
    created: int = 0
    url: str = ""
    content: str = ""  # html
    images: list[str] = field(default_factory=list)
    thumb: str = ""  # zvideo thumbnail
    video_url: str = ""  # zvideo play url
    qid: str = ""  # question id (answers)
    aid: str = ""  # answer id (answers / articles)
    rank: int = 0  # hot-list ranking (0 = not a ranked feed)
    search_match: bool = False  # True for search results (excerpt has <em>)

    @property
    def badge(self) -> str:
        return {
            "answer": "回答",
            "article": "文章",
            "zvideo": "视频",
            "question": "提问",
            "pin": "想法",
        }.get(self.kind, self.kind)


@dataclass
class Comment:
    id: str = ""
    author: str = ""
    content: str = ""
    votes: int = 0
    created: int = 0
    reply_to: str = ""


def fmt_num(n: int | None) -> str:
    n = n or 0
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_time(ts: int | None) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d / 60)}分钟前"
    if d < 86400:
        return f"{int(d / 3600)}小时前"
    if d < 86400 * 7:
        return f"{int(d / 86400)}天前"
    return time.strftime("%m-%d", time.localtime(ts))


# --------------------------------------------------------------------------
# HTML helpers
# --------------------------------------------------------------------------


def _img_src(tag_text: str) -> str:
    for attr in ("data-actualsrc", "data-original", "data-src", "src"):
        m = re.search(
            attr + r'\s*=\s*(["\'])(.*?)\1', tag_text, re.I
        )
        if m:
            u = m.group(2).strip()
            if u and not u.startswith("data:"):
                return urljoin(BASE, u)
    return ""


def extract_images(html: str) -> list[str]:
    """Fast regex-based image url extraction from content html (for cards)."""
    out: list[str] = []
    for tag in _IMG_RE.findall(html or ""):
        u = _img_src(tag)
        if u and u not in out:
            out.append(u)
    return out[:20]


def strip_html(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html or "", "lxml").get_text(" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")


# --------------------------------------------------------------------------
# Feed item normalization
# --------------------------------------------------------------------------


def _author(target: dict) -> str:
    a = target.get("author") or {}
    return a.get("name") or ""


def _time(target: dict) -> int:
    for k in ("created_time", "created", "published_at"):
        v = target.get(k)
        if v:
            return int(v)
    return 0


def _excerpt(target: dict, content: str = "") -> str:
    e = target.get("excerpt") or target.get("detail") or ""
    if not e and content:
        e = strip_html(content)
    return (e or "")[:200]


def _hot_rank(raw: dict, index: int) -> int:
    """Parse the hot-list rank from the raw item's ``detail_text`` (e.g.
    ``"1"`` / ``"2"``), falling back to the list position."""
    m = re.match(r"\s*(\d+)", str(raw.get("detail_text") or ""))
    return int(m.group(1)) if m else index


def normalize_item(raw: dict) -> FeedItem | None:
    typ = raw.get("type") or ""
    target = raw.get("target") or {}

    # skip ads
    if (
        raw.get("ad_info")
        or target.get("ad_answer_info")
        or target.get("ad_info")
        or "ad" in str(target.get("extra", "")).lower()[:50]
    ):
        return None

    # some feeds nest the real object under "target"
    if typ == "feed":
        typ = target.get("type") or ""
    elif not target:
        target = raw
        typ = raw.get("type") or ""

    if typ == "answer":
        q = target.get("question") or {}
        content = target.get("content") or ""
        qid = q.get("id") or ""
        tid = target.get("id") or ""
        url = target.get("url") or ""
        if qid and tid:
            url = f"{BASE}/question/{qid}/answer/{tid}"
        item = FeedItem(
            kind="answer",
            rid=str(raw.get("id") or ""),
            title=(q.get("title") or target.get("title") or ""),
            author=_author(target),
            excerpt=_excerpt(target, content),
            votes=int(target.get("voteup_count") or 0),
            comments=int(target.get("comment_count") or 0),
            created=_time(target),
            url=url,
            content=content,
            images=extract_images(content),
            qid=str(qid),
            aid=str(tid),
        )
    elif typ == "article":
        content = target.get("content") or ""
        tid = target.get("id") or ""
        url = target.get("url") or ""
        if tid:
            url = f"https://zhuanlan.zhihu.com/p/{tid}"
        item = FeedItem(
            kind="article",
            rid=str(raw.get("id") or ""),
            title=(target.get("title") or ""),
            author=_author(target),
            excerpt=_excerpt(target, content),
            votes=int(target.get("voteup_count") or 0),
            comments=int(target.get("comment_count") or 0),
            created=_time(target),
            url=url,
            content=content,
            images=extract_images(content),
            aid=str(tid),
        )
    elif typ == "zvideo":
        video = target.get("video") or {}
        thumb = video.get("thumbnail") or ""
        play = ""
        for quality in ("hd", "sd", "ld"):
            q = (video.get("playlist") or {}).get(quality) or {}
            if q.get("play_url"):
                play = q["play_url"]
                break
        tid = target.get("id") or ""
        item = FeedItem(
            kind="zvideo",
            rid=str(raw.get("id") or ""),
            title=(target.get("title") or target.get("excerpt_title") or ""),
            author=_author(target),
            excerpt=(target.get("excerpt") or "")[:200],
            votes=int(target.get("play_count") or 0),
            comments=int(target.get("comment_count") or 0),
            created=_time(target),
            url=f"{BASE}/zvideo/{tid}" if tid else (target.get("url") or ""),
            images=[thumb] if thumb else [],
            thumb=thumb,
            video_url=play,
        )
    elif typ == "question":
        tid = target.get("id") or ""
        item = FeedItem(
            kind="question",
            rid=str(raw.get("id") or ""),
            title=(target.get("title") or ""),
            author=_author(target),
            excerpt=_excerpt(target),
            votes=int(target.get("answer_count") or 0),
            comments=int(target.get("comment_count") or 0),
            created=_time(target),
            url=f"{BASE}/question/{tid}" if tid else (target.get("url") or ""),
            qid=str(tid),
        )
    elif typ == "pin":
        content = target.get("content") or ""
        pin_images = [
            (im.get("url") or "")
            for im in (target.get("images") or [])
            if im.get("url")
        ]
        images = pin_images + [u for u in extract_images(content) if u not in pin_images]
        item = FeedItem(
            kind="pin",
            rid=str(raw.get("id") or ""),
            title=(target.get("title") or target.get("excerpt_title") or "想法"),
            author=_author(target),
            excerpt=(target.get("excerpt") or strip_html(content))[:200],
            votes=0,
            comments=int(target.get("comment_count") or 0),
            created=_time(target),
            url=f"{BASE}/pin/{target.get('id') or ''}" or (target.get("url") or ""),
            content=content,
            images=images[:20],
        )
    else:
        return None

    if not item.title and not item.content:
        return None
    return item


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class ZhihuSession:
    def __init__(self, cookies: dict | None = None):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Referer": f"{BASE}/",
                "Accept": "application/json, text/plain, */*",
            }
        )
        if cookies:
            self.s.cookies.update(cookies)
        self._next_url: str = ""

    def me(self) -> dict | None:
        try:
            r = self.s.get(ME_URL, timeout=10)
            if r.status_code == 200:
                j = r.json()
                if j.get("id") or j.get("name"):
                    return j
        except Exception:
            pass
        return None

    def is_logged_in(self) -> bool:
        return self.me() is not None

    def feed(self, after_id: str = "", page: int = 1, limit: int = 20):
        if not after_id:
            self._next_url = ""
        if self._next_url:
            r = self.s.get(self._next_url, timeout=15)
        else:
            r = self.s.get(
                FEED_URL,
                params={
                    "action": "down",
                    "desktop": "true",
                    "page_number": page,
                    "limit": limit,
                },
                timeout=15,
            )
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        items = []
        for raw in j.get("data", []):
            item = normalize_item(raw)
            if item:
                items.append(item)
        paging = j.get("paging") or {}
        self._next_url = paging.get("next") or ""
        is_end = bool(paging.get("is_end")) or not items
        return items, is_end

    def hot_list(self, limit: int = 50):
        """Zhihu 热榜 (single ranked page; no pagination).

        Returns (items, is_end).  Each item carries its rank number.
        """
        r = self.s.get(
            f"{BASE}/api/v3/feed/topstory/hot-list-web",
            params={"limit": limit, "desktop": "true"},
            timeout=15,
        )
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        items = []
        for idx, raw in enumerate(j.get("data", []), 1):
            target = raw.get("target") or {}
            if raw.get("type") == "hot_list_feed":
                link = (target.get("link") or {}).get("url") or ""
                match = re.search(r"/question/(\d+)", link)
                image = (target.get("image_area") or {}).get("url") or ""
                item = FeedItem(
                    kind="question",
                    rid=str(raw.get("id") or raw.get("card_id") or ""),
                    title=(target.get("title_area") or {}).get("text") or "",
                    excerpt=(target.get("excerpt_area") or {}).get("text") or "",
                    url=link,
                    images=[image] if image else [],
                    qid=match.group(1) if match else "",
                    rank=idx,
                )
            else:
                item = normalize_item(raw)
                if item:
                    item.rank = _hot_rank(raw, idx)
            if item and item.title:
                items.append(item)
        return items, True

    # ------------------------------------------------------------------
    # open a specific zhihu page by URL (command line / Ctrl+O)
    # ------------------------------------------------------------------

    def fetch_url(self, url: str) -> FeedItem:
        """Resolve a zhihu URL into a content item for the viewer."""
        url = (url or "").strip()
        m = re.search(r"zhihu\.com/question/(\d+)(?:/answer/(\d+))?", url)
        if m:
            qid, aid = m.group(1), m.group(2)
            if aid:
                return self._fetch_answer(aid, qid)
            return self._fetch_question(qid)
        m = re.search(r"(?:zhuanlan\.zhihu\.com/p/|zhihu\.com/(?:p|article)/)(\d+)", url)
        if m:
            return self._fetch_article(m.group(1))
        m = re.search(r"zhihu\.com/zvideo/(\d+)", url)
        if m:
            return self._fetch_zvideo(m.group(1))
        raise ZhihuError("无法识别的知乎链接（支持 question / answer / zhuanlan / zvideo）")

    def _fetch_answer(self, aid: str, qid: str = "") -> FeedItem:
        r = self.s.get(
            f"{BASE}/api/v4/answers/{aid}",
            params={"include": self._ANSWERS_INCLUDE},
            timeout=15,
        )
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        q = j.get("question") or {}
        qid = str(q.get("id") or qid)
        content = j.get("content") or ""
        item = FeedItem(
            kind="answer",
            rid=str(j.get("id") or aid),
            title=(q.get("title") or j.get("title") or ""),
            author=(j.get("author") or {}).get("name") or "",
            excerpt=j.get("excerpt") or "",
            votes=int(j.get("voteup_count") or 0),
            comments=int(j.get("comment_count") or 0),
            created=int(j.get("created_time") or 0),
            url=f"{BASE}/question/{qid}/answer/{j.get('id') or aid}",
            content=content,
            images=extract_images(content),
            qid=qid,
            aid=str(j.get("id") or aid),
        )
        return item

    def _fetch_question(self, qid: str) -> FeedItem:
        r = self.s.get(f"{BASE}/api/v4/questions/{qid}", timeout=15)
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        item = FeedItem(
            kind="question",
            rid=str(qid),
            title=j.get("title") or "",
            author=(j.get("author") or {}).get("name") or "",
            excerpt=j.get("detail") or "",
            votes=int(j.get("voteup_count") or 0),
            comments=int(j.get("comment_count") or 0),
            created=int(j.get("created_time") or 0),
            url=f"{BASE}/question/{qid}",
            qid=str(qid),
        )
        return item

    def _fetch_article(self, pid: str) -> FeedItem:
        r = self.s.get(f"{BASE}/api/v4/articles/{pid}", timeout=15)
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        content = j.get("content") or ""
        return FeedItem(
            kind="article",
            rid=str(pid),
            title=j.get("title") or "",
            author=(j.get("author") or {}).get("name") or "",
            excerpt=j.get("excerpt") or "",
            votes=int(j.get("voteup_count") or 0),
            comments=int(j.get("comment_count") or 0),
            created=int(j.get("created_time") or 0),
            url=f"https://zhuanlan.zhihu.com/p/{pid}",
            content=content,
            images=extract_images(content),
            aid=str(pid),
        )

    def _fetch_zvideo(self, zid: str) -> FeedItem:
        r = self.s.get(f"{BASE}/api/v4/zvideos/{zid}", timeout=15)
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        video = j.get("video") or {}
        thumb = video.get("thumbnail") or ""
        play = ""
        for quality in ("hd", "sd", "ld"):
            q = (video.get("playlist") or {}).get(quality) or {}
            if q.get("play_url"):
                play = q["play_url"]
                break
        content = j.get("content") or ""
        return FeedItem(
            kind="zvideo",
            rid=str(zid),
            title=j.get("title") or j.get("excerpt_title") or "",
            author=(j.get("author") or {}).get("name") or "",
            excerpt=(j.get("excerpt") or "")[:200],
            votes=int(j.get("play_count") or 0),
            comments=int(j.get("comment_count") or 0),
            created=int(j.get("created_time") or 0),
            url=f"{BASE}/zvideo/{zid}",
            content=content,
            images=[thumb] if thumb else [],
            thumb=thumb,
            video_url=play,
        )

    # ------------------------------------------------------------------
    # search (Ctrl+S)
    # ------------------------------------------------------------------

    def search(self, query: str, offset: int = 0, limit: int = 20):
        """Search zhihu (general: answers/questions/articles/zhuanlan).

        Returns (items, is_end, next_offset).  ``query`` is re-quoted on
        every call so both the initial page and follow-up pages work.
        """
        r = self.s.get(
            f"{BASE}/api/v4/search_v3",
            params={
                "t": "general",
                "q": query,
                "correction": "1",
                "offset": offset,
                "limit": limit,
                "filter_fields": "",
            },
            timeout=15,
        )
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        items: list[FeedItem] = []
        seen: set[str] = set()
        for raw in j.get("data", []):
            obj = raw.get("object") or raw
            item = normalize_item(obj)
            if not item:
                continue
            item.search_match = True
            key = item.url or item.rid
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        paging = j.get("paging") or {}
        is_end = bool(paging.get("is_end")) or not items
        return items, is_end, offset + len(items)

    def fetch_image_bytes(self, url: str, timeout: int = 30) -> bytes:
        data = _image_cache_get(url)
        if data is not None:
            return data
        r = self.s.get(
            url,
            timeout=timeout,
            headers={
                "Referer": f"{BASE}/",
                "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
            },
        )
        r.raise_for_status()
        data = r.content
        head = data.lstrip()[:2048].lower()
        if b"<svg" in head:
            try:
                converted = subprocess.run(
                    ["convert", "svg:-", "png:-"], input=data,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=True, timeout=20,
                ).stdout
            except (OSError, subprocess.SubprocessError) as exc:
                raise ZhihuError(f"SVG 转 PNG 失败：{exc}") from exc
            if not converted:
                raise ZhihuError("SVG 转 PNG 失败：转换结果为空")
            data = converted
        _image_cache_put(url, data)
        return data

    # ------------------------------------------------------------------
    # question answers & comments
    # ------------------------------------------------------------------

    _ANSWERS_INCLUDE = (
        "data[*].is_normal,admin_closed_comment,reward_info,is_collapsed,"
        "annotation_action,annotation_detail,collapse_reason,is_sticky,"
        "collapsed_by,suggest_edit,comment_count,can_comment,content,"
        "editable_content,attachment,voteup_count,reshipment_settings,"
        "comment_permission,created_time,updated_time,review_info,"
        "relevant_info,question,excerpt,is_labeled,label_info,"
        "relationship.is_authorized,is_author,voting,is_thanked,is_nothelp,"
        "is_recognized,is_eligible_extension_author,is_org,mark_infos"
    )

    def question_answers(
        self, qid: str, offset: int = 0, limit: int = 20
    ) -> tuple[list[FeedItem], bool]:
        """Answers of a question (default sort). Returns (items, is_end)."""
        r = self.s.get(
            f"{BASE}/api/v4/questions/{qid}/answers",
            params={
                "include": self._ANSWERS_INCLUDE,
                "limit": limit,
                "offset": offset,
                "platform": "desktop",
                "sort_by": "default",
            },
            timeout=15,
        )
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        items = [
            it
            for it in (normalize_item(raw) for raw in (j.get("data") or []))
            if it
        ]
        paging = j.get("paging") or {}
        is_end = bool(paging.get("is_end")) or not items
        return items, is_end

    def answer_comments(
        self, aid: str, offset: int = 0, limit: int = 20
    ) -> tuple[list[Comment], bool]:
        """Comments of an answer. Returns (comments, is_end)."""
        r = self.s.get(
            f"{BASE}/api/v4/answers/{aid}/comments",
            params={
                "include": (
                    "data[*].author,content,vote_count,comment_count,"
                    "created_time,reply_to_author"
                ),
                "limit": limit,
                "offset": offset,
                "status": "open",
            },
            timeout=15,
        )
        if r.status_code in (401, 403):
            raise NotLoggedIn("登录已过期，请重新登录：zhihu --login")
        r.raise_for_status()
        j = r.json()
        comments = [
            self._normalize_comment(raw) for raw in (j.get("data") or [])
        ]
        comments = [c for c in comments if c.content]
        paging = j.get("paging") or {}
        is_end = bool(paging.get("is_end")) or not comments
        return comments, is_end

    @staticmethod
    def _normalize_comment(raw: dict) -> Comment:
        author = (raw.get("author") or {}).get("name") or ""
        reply_to = (raw.get("reply_to_author") or {}).get("name") or ""
        return Comment(
            id=str(raw.get("id") or ""),
            author=author,
            content=strip_html(raw.get("content") or "").strip()[:500],
            votes=int(raw.get("vote_count") or 0),
            created=int(raw.get("created_time") or 0),
            reply_to=reply_to,
        )


# --------------------------------------------------------------------------
# Cookie persistence
# --------------------------------------------------------------------------


def parse_cookie_header(s: str) -> dict:
    out: dict[str, str] = {}
    for part in s.replace("\n", ";").split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            if k and v:
                out[k.strip()] = v.strip()
    return out


def parse_netscape(path: Path) -> dict:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 7:
                out[parts[5]] = parts[6]
    except Exception:
        pass
    return out


def load_cookies() -> dict:
    if COOKIE_FILE.exists():
        try:
            return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if LEGACY_COOKIE_FILE.exists():
        return parse_netscape(LEGACY_COOKIE_FILE)
    return {}


def save_cookies(cookies: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Browser cookie extraction (Firefox plaintext, Chrome-family decryption)
# --------------------------------------------------------------------------

FIREFOX_DIRS = ["~/.mozilla/firefox", "~/.librewolf", "~/.waterfox"]
CHROME_DIRS = [
    "google-chrome",
    "chromium",
    "microsoft-edge",
    "brave-browser",
    "vivaldi",
    "zen",
]


def _read_firefox_cookies(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT name, value FROM moz_cookies WHERE host_key LIKE '%zhihu.com'"
        ).fetchall()
    finally:
        con.close()
    return {name: value for name, value in rows if name and value}


def _chrome_keys(base_dir: Path) -> tuple[bytes | None, bytes | None]:
    """Best-effort Chrome cookie encryption keys for Linux (v10/v11)."""
    try:
        from Crypto.Cipher import AES
        from Crypto.Protocol.KDF import PBKDF2

        state = json.loads((base_dir / "Local State").read_text(encoding="utf-8"))
        blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
        if not blob.startswith(b"DPAPI"):
            return None, None
        ct = blob[5:]
        derived = PBKDF2(b"peanuts", b"peanuts", 32, count=1)
        key10 = key11 = None
        try:  # v11: AES-GCM wrapped, nonce(12)+ct+tag(16)
            nonce, tag = ct[:12], ct[-16:]
            key11 = AES.new(derived, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
                ct[12:-16], tag
            )
        except Exception:
            pass
        try:  # v10: AES-CBC, first 32 bytes of decrypted blob
            plain = AES.new(derived, AES.MODE_CBC, IV=b" " * 16).decrypt(ct)
            if len(plain) >= 32:
                key10 = plain[:32]
        except Exception:
            pass
        return key10, key11
    except Exception:
        return None, None


def _decrypt_chrome_cookie(value: bytes, key10, key11) -> str | None:
    if not value:
        return None
    try:
        from Crypto.Cipher import AES

        if value.startswith(b"v10") and key10:
            nonce, tag = value[3:15], value[-16:]
            return AES.new(key10, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
                value[15:-16], tag
            ).decode()
        if value.startswith(b"v11") and key11:
            nonce, tag = value[3:15], value[-16:]
            return AES.new(key11, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
                value[15:-16], tag
            ).decode()
    except Exception:
        pass
    return None


def _read_chrome_cookies(db: Path, key10, key11) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT name, encrypted_value, value FROM cookies "
            "WHERE host_key LIKE '%zhihu.com'"
        ).fetchall()
    finally:
        con.close()
    out: dict[str, str] = {}
    for name, enc, val in rows:
        if val:
            out[name] = val
            continue
        d = _decrypt_chrome_cookie(enc, key10, key11)
        if d:
            out[name] = d
    return out


def browser_cookies() -> dict:
    """Try to pull zhihu cookies from installed browsers (best effort)."""
    out: dict[str, str] = {}
    for d in FIREFOX_DIRS:
        base = Path(d).expanduser()
        if not base.exists():
            continue
        for db in sorted(base.glob("*/cookies.sqlite")):
            try:
                out.update(_read_firefox_cookies(db))
            except Exception:
                continue
    if not out:
        for name in CHROME_DIRS:
            base = Path.home() / ".config" / name
            if not base.exists():
                continue
            key10, key11 = _chrome_keys(base)
            if not (key10 or key11):
                continue
            for db in sorted(base.glob("*/Cookies")):
                try:
                    out.update(_read_chrome_cookies(db, key10, key11))
                except Exception:
                    continue
    return out


# --------------------------------------------------------------------------
# Login flow (console, before TUI)
# --------------------------------------------------------------------------


def _kbhit(timeout: float = 0.1) -> bool:
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    return bool(r)


def login_flow() -> dict:
    print("=== 知乎 CLI 登录 ===")
    print("[1/3] 尝试从浏览器读取 Cookie…")
    cookies = browser_cookies()
    if cookies:
        s = ZhihuSession(cookies)
        user = s.me()
        if user:
            save_cookies(cookies)
            print(f"  已从浏览器读取登录态：{user.get('name', '')}")
            return cookies
        print("  浏览器 Cookie 无效，继续…")
    else:
        print("  未在浏览器中找到知乎 Cookie")

    print("[2/3] 自动打开浏览器登录页面…")
    try:
        webbrowser.open(f"{BASE}/signin")
    except Exception:
        pass
    print("  请在浏览器中完成登录。正在等待 Cookie（最多 90 秒），按回车可跳过等待。")
    deadline = time.time() + 90
    while time.time() < deadline:
        cookies = browser_cookies()
        if cookies:
            s = ZhihuSession(cookies)
            user = s.me()
            if user:
                save_cookies(cookies)
                print(f"  登录成功：{user.get('name', '')}")
                return cookies
        if _kbhit():
            sys.stdin.readline()
            break
        time.sleep(2)

    print("[3/3] 手动粘贴 Cookie（备用方案）")
    print("  浏览器登录 zhihu.com 后，从开发者工具或 Cookie 扩展复制")
    print("  Cookie 字符串，形如：d_c0=xxx; z_c0=yyy")
    try:
        line = input("  Cookie > ").strip()
    except (EOFError, KeyboardInterrupt):
        raise ZhihuError("已取消登录")
    cookies = parse_cookie_header(line)
    if cookies:
        s = ZhihuSession(cookies)
        user = s.me()
        if user:
            save_cookies(cookies)
            print(f"  登录成功：{user.get('name', '')}")
            return cookies
    raise ZhihuError("登录失败：Cookie 无效或已过期")
