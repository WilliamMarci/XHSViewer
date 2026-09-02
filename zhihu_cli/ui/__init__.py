"""Textual UI package: split by page/component.

Exposes the same public names the old single-file ``ui.py`` did, so
``from .ui import ZhihuApp`` and similar imports keep working.
"""
from __future__ import annotations

from .constants import (
    BADGE_COLOR,
    CUSTOM_THEMES,
    ICON_COMMENT,
    ICON_COPY,
    ICON_IMAGE,
    ICON_IMAGES,
    ICON_LINK,
    ICON_PLAY,
)
from .widgets import (
    AnswerHeader,
    AnswerTopBar,
    BottomBar,
    Card,
    ClickableText,
    CodeHead,
    CommentScroll,
    ContentScroll,
    FeedScroll,
    FeedTopBar,
    HistoryAnswerRow,
    HistoryEndRule,
    HistoryEntry,
    HistoryHeading,
    LinkButton,
    PageSearchBar,
    StatusLine,
    TopBar,
    _close_page_search,
    _find_in_widgets,
    _open_page_search,
    _set_search_highlight,
    _truncate_cells,
    open_image_with_system,
    push_item_screen,
    system_viewer_notice,
)
from .feed import FeedScreen, SearchScreen
from .question import QuestionScreen
from .image import ImageSection, ImageViewerScreen, ViewerHint, _placeholder_text
from .content import ContentScreen
from .comments import CommentsScreen
from .modals import SearchPromptScreen, TableScreen, UrlPromptScreen
from .app import ZhihuApp

__all__ = [
    "BADGE_COLOR",
    "CUSTOM_THEMES",
    "ICON_COMMENT",
    "ICON_COPY",
    "ICON_IMAGE",
    "ICON_IMAGES",
    "ICON_LINK",
    "ICON_PLAY",
    "ZhihuApp",
    "FeedScreen",
    "SearchScreen",
    "QuestionScreen",
    "ContentScreen",
    "CommentsScreen",
    "ImageViewerScreen",
    "ImageSection",
    "ViewerHint",
    "SearchPromptScreen",
    "TableScreen",
    "UrlPromptScreen",
    "TopBar",
    "AnswerTopBar",
    "FeedTopBar",
    "StatusLine",
    "BottomBar",
    "AnswerHeader",
    "Card",
    "HistoryAnswerRow",
    "HistoryHeading",
    "HistoryEntry",
    "HistoryEndRule",
    "PageSearchBar",
    "FeedScroll",
    "ClickableText",
    "LinkButton",
    "CodeHead",
    "ContentScroll",
    "CommentScroll",
    "open_image_with_system",
    "system_viewer_notice",
    "push_item_screen",
    "_placeholder_text",
    "_truncate_cells",
    "_open_page_search",
    "_set_search_highlight",
    "_close_page_search",
    "_find_in_widgets",
]
