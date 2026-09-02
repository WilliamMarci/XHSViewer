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