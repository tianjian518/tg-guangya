"""关键词过滤与文件名解析。

职责：
1. 关键词过滤：include（命中任意一个才提交）/ exclude（命中任意一个则跳过）；
2. 分辨率过滤：min_resolution 指定最低画质（如 1080P / 2160P）；
3. 标题解析（可选）：借助 guessit 从文件名提取 片名/年份/季集/分辨率，
   解析失败时退化为简单规则，保证不依赖第三方库也能跑。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_RES_PATTERNS = [
    (2160, re.compile(r"2160p|4k|uhd", re.I)),
    (1440, re.compile(r"1440p", re.I)),
    (1080, re.compile(r"1080p|fhd", re.I)),
    (720, re.compile(r"720p|hd", re.I)),
    (480, re.compile(r"480p", re.I)),
]
_RES_NAME = {2160: "2160P/4K", 1440: "1440P", 1080: "1080P", 720: "720P", 480: "480P"}


def parse_resolution(text: str) -> int:
    """返回分辨率等级的数值（越大越高），未识别为 0。"""
    best = 0
    for level, pat in _RES_PATTERNS:
        if pat.search(text or ""):
            best = max(best, level)
    return best


class KeywordFilter:
    def __init__(
        self,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        min_resolution: str = "",
    ) -> None:
        self.include = [str(x).strip().lower() for x in (include or []) if x]
        self.exclude = [str(x).strip().lower() for x in (exclude or []) if x]
        self.min_res = parse_resolution(min_resolution) if min_resolution else 0

    def match(self, text: str) -> tuple[bool, str]:
        """返回 (是否放行, 原因)。text 为消息正文 + 标题。"""
        low = (text or "").lower()
        if self.exclude and any(k in low for k in self.exclude):
            return False, "命中排除词"
        if self.include and not any(k in low for k in self.include):
            return False, "未命中包含词"
        if self.min_res and parse_resolution(text) < self.min_res:
            return False, f"分辨率低于 {_RES_NAME.get(self.min_res, str(self.min_res))}"
        return True, "通过"


def parse_title(filename: str) -> dict:
    """尽力解析影视文件名，返回结构化信息。失败返回空字段。"""
    info = {
        "title": "", "year": 0, "kind": "other",
        "season": 0, "episode": 0, "resolution": "",
    }
    if not filename:
        return info
    try:
        from guessit import guessit  # 可选依赖
        g = guessit(filename)
        info["title"] = str(g.get("title") or "")
        info["year"] = int(g.get("year") or 0)
        info["kind"] = str(g.get("type") or "other").lower()
        info["season"] = int(g.get("season") or 0)
        info["episode"] = int((g.get("episode") or 0))
        res = g.get("screen_size")
        if res:
            info["resolution"] = str(res)
    except ImportError:
        log.debug("guessit 未安装，使用简单解析")
    except Exception as exc:
        log.debug("guessit 解析失败: %s", exc)
    # 兜底：自己抠分辨率
    if not info["resolution"]:
        lvl = parse_resolution(filename)
        if lvl:
            info["resolution"] = _RES_NAME.get(lvl, f"{lvl}p")
    return info
