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


def build_cn_filename(title: str, ext: str = "") -> str:
    """从 Telegram 标题构造干净的云盘文件夹/文件名（无扩展名）。

    例：
      "【电影】黑夜告白 2026 2160p 高清中字"   -> "黑夜告白.2026"
      "Oppenheimer 2023 4K WEB-DL"            -> "Oppenheimer.2023"
      "庆余年 第2季 第3集 1080p 中字"          -> "庆余年.S02E03"
    """
    name = folder_name(title) if title else ""
    if not name:
        return ""
    if ext:
        if not ext.startswith("."):
            ext = "." + ext
        name += ext
    # 超长截断（中文 UTF-8 3 字节，安全线 240 字节）
    if len(name.encode("utf-8")) > 240:
        name = name[:60] + (ext or "")
    return _ILLEGAL.sub("", name)


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
