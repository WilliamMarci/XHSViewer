"""User configuration (JSON in ~/.config/xhs-viewer/config.json)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "xhs-viewer"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    # 进入笔记后自动内嵌显示图库第一张图片（图片社区默认打开即看图）
    "expand_images_by_default": True,
    # 列表模式下卡片内显示 3:4 封面图（需要终端支持 Kitty 图片协议）
    "feed_show_images": False,
    # 卡片列表视图：list（纵向卡片）/ grid（宫格，需终端支持图片）
    "feed_view": "list",
    # 宫格单元格大小：small / medium / large / xlarge
    # 列数由终端宽度和单元格大小自动决定
    "feed_grid_cell": "xlarge",
    # 图文笔记正文顺序：True = 图片集在前（Photo first），False = 文字在前
    "note_photo_first": True,
    "history_enabled": True,
    # False: /tmp/xhsviewer/history.json; True: config directory.
    "history_persistent": False,
    "auto_refresh_recommend": False,
    # 正文底部「尾端按钮」：browser（在浏览器中打开）/ next_note（下一条笔记）/ none（隐藏）
    "content_end_button": "browser",
    # 缓存根目录；为空时使用系统临时目录下的 xhsviewer（/tmp/xhsviewer）
    "cache_dir": "",
}


def cache_root() -> Path:
    """Cache root: config ``cache_dir`` when set, else ``/tmp/xhsviewer``."""
    override = str(get("cache_dir", "") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(tempfile.gettempdir()) / "xhsviewer"


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
