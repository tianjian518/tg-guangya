"""把 Telegram 标题转成干净的云盘【外层文件夹名】。

命名逻辑统一收口到 core/ident.py 的 analyze()：
- 片名主体（去类型标签 / 分辨率 / 编码 / 语言字幕 / 广告词 / 下载器尾巴）；
- 电影 / 多集包：片名.年份（年份可辨翻拍重制）；
- 单集 / 季：片名.SxxExx / 片名.Sxx（追剧每集独立文件夹，互不覆盖）。

确定性是命名的第一要求：同一内容无论来自哪个频道、哪种写法，folder 必须一致，
云端/本地账本才能靠它认出「转过了」。名字好不好看是次要的。

本模块只保留 build_cn_filename 薄封装（带扩展名与超长截断）供旧调用点使用。
"""
from __future__ import annotations

import re

from core.ident import folder_name

# 文件系统非法字符（Windows / 多数网盘通用）
_ILLEGAL = re.compile(r'[\\/:*?"<>|]')

# 电驴/磁力的内容指纹（32 位 md4 / 40 位 sha1）：频道常把整条链接贴进正文，
# 标题解析会把这串 hex 当成片名的一部分（实测「婚姻之后2020007923...4B8F」），
# 必须剥掉——否则 12 集会全部落成一个带 hash 的同名文件。
_HEXHASH = re.compile(r"(?<![0-9a-zA-Z])(?:[0-9a-f]{32}|[0-9a-f]{40})(?![0-9a-zA-Z])", re.I)
# 剥离后残留的空分隔符（连续的点/下划线/破折号）
_EMPTY_SEP = re.compile(r"(?:[._\-]\s*){2,}")

# 整条下载链接（磁力/电驴/迅雷/http）：频道正文常把链接直接贴出来，
# 链接里的 size 段与 hash 会粘成一片（…|123|20200079…4B8F），逐段剥离很难干净，
# 所以命名前先把整条链接摘掉，真实文件名另由 link_filename() 从链接里取。
_URL = re.compile(
    r"(?:magnet|ed2k|thunder|flashget|qqdl|ftp|https?)[:：]\?\S*"
    r"|(?:magnet|ed2k|thunder|flashget|qqdl|ftp|https?)[:：]//[^\s，,。；;]+",
    re.I)

# 链接里的真实文件名（命名权威来源，比频道发帖标题可靠）
_ED2K_NAME = re.compile(r"ed2k://\|file\|([^|]+)\|", re.I)
_MAGNET_DN = re.compile(r"[?&]dn=([^&]+)", re.I)


def link_filename(url: str) -> str:
    """从链接本身取出真实文件名（ed2k 的 |file| 段 / 磁力的 dn / http 末段）。

    返回空串表示取不到（调用方应退回用发帖标题）。
    """
    if not url:
        return ""
    m = _ED2K_NAME.search(url)
    if m:
        return _unquote(m.group(1)).strip()
    m = _MAGNET_DN.search(url)
    if m:
        return _unquote(m.group(1)).strip()
    if url.lower().startswith(("http://", "https://")):
        tail = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        return _unquote(tail).strip()
    return ""


def _unquote(s: str) -> str:
    from urllib.parse import unquote, unquote_plus
    out = unquote_plus(s)
    if "%" in out:
        out = unquote(out)
    return out


def build_cn_filename(title: str, ext: str = "") -> str:
    """从 Telegram 标题构造干净的云盘文件夹/文件名（无扩展名）。

    例：
      "【电影】黑夜告白 2026 2160p 高清中字"   -> "黑夜告白.2026"
      "Oppenheimer 2023 4K WEB-DL"            -> "Oppenheimer.2023"
      "庆余年 第2季 第3集 1080p 中字"          -> "庆余年.S02E03"
    """
    src = _URL.sub(" ", title or "").strip()
    name = folder_name(src) if src else ""
    if not name and title:
        name = folder_name(title) or ""
    if not name:
        return ""
    # 剥掉误入片名的电驴/磁力指纹串（32/40 位纯 hex）
    stripped = _EMPTY_SEP.sub(lambda m: m.group(0)[0], _HEXHASH.sub("", name))
    stripped = stripped.strip(" ._-")
    if stripped:
        name = stripped
    if ext:
        if not ext.startswith("."):
            ext = "." + ext
        name += ext
    # 超长截断（中文 UTF-8 3 字节，安全线 240 字节）
    if len(name.encode("utf-8")) > 240:
        name = name[:60] + (ext or "")
    return _ILLEGAL.sub("", name)


def build_cn_filename_from(url: str, title: str, ext: str = "") -> str:
    """命名双源：发帖标题出中文译名，链接出集号/季数等真实信息。

    电驴/磁力常一集一条链接（S01E01…S01E12），而每条链接的发帖文本是同一段话，
    只按标题命名会让 12 集落成一个同名文件（互相覆盖）。这里从链接真实名里
    把集号补回来；补不上就退回纯标题命名。
    """
    base = build_cn_filename(title, ext)
    lf = link_filename(url)
    if not lf:
        return base
    if not base:
        return build_cn_filename(lf, ext)
    # 单集/整季签名：S01E09 / S01 / S01-S12
    m = re.search(r"(?i)\bs(\d{1,2})(?:\s*[-_. ]?\s*e(\d{1,3}))?(?:\s*-\s*s?\d{1,2})?\b", lf)
    if not m:
        return base
    if m.group(2):
        sig = "S%02dE%02d" % (int(m.group(1)), int(m.group(2)))
    else:
        sig = "S%02d" % int(m.group(1))
    if sig.lower() in base.lower():
        return base
    sep = " " if base.endswith(")") else "."
    return base + sep + sig


if __name__ == "__main__":
    tests = [
        ("【电影】黑夜告白 2026 2160p 高清中字", "mp4"),
        ("Oppenheimer 2023 4K WEB-DL", "mkv"),
        ("庆余年 第2季 第3集 1080p 中字", "mp4"),
        ("消失的她 2023 1080P 国语中英字幕", "mp4"),
        ("【动漫】国漫 凡人修仙传 2160p 更新至第88集", "mp4"),
        ("流浪地球2 2023 2160p 4K 完整版 未删减", "mkv"),
    ]
    for t, e in tests:
        print(f"{t!r:55} -> {build_cn_filename(t, e)!r}")
