"""User configuration (JSON in ~/.config/zhihu-viewer/config.json)."""
from __future__ import annotations

import json
import os
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
    # False: /tmp/zhihu-viewer/history.json; True: config directory.
    "history_persistent": False,
    "auto_refresh_recommend": False,
}


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
