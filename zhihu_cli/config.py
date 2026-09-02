"""User configuration (JSON in ~/.config/zhihu-viewer/config.json)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "zhihu-viewer"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    # 知乎行间公式渲染出的 SVG 图片无法在终端展示，默认隐藏（公式仍以
    # 文本/utftex 形式渲染）；设为 true 时将其作为普通图片展示
    "show_math_svg": False,
    "expand_images_by_default": False,
    "history_enabled": True,
    # False: /tmp/zhihuviewer/history.json; True: config directory.
    "history_persistent": False,
    "auto_refresh_recommend": False,
    # 正文底部「尾端按钮」：browser（在浏览器中打开）/ next_answer（下一个回答）/ none（隐藏）
    "content_end_button": "browser",
    # 缓存根目录；为空时使用系统临时目录下的 zhihuviewer（/tmp/zhihuviewer）
    "cache_dir": "",
}


def cache_root() -> Path:
    """Cache root: config ``cache_dir`` when set, else ``/tmp/zhihuviewer``."""
    override = str(get("cache_dir", "") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / "zhihuviewer"


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:
        pass
    return cfg


def get(key: str, default=None):
    return load().get(key, default)


def set_value(key: str, value) -> None:
    """Persist one supported setting while preserving the other values."""
    if key not in DEFAULTS:
        raise KeyError(key)
    cfg = load()
    cfg[key] = value
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
