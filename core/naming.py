"""把 Telegram 中文标题转成干净的云盘文件名（如《黑夜告白》→ 黑夜告白.2026.2160p.mp4）。

设计原则：
- 失败安全：只做「提取 + 清洗」，不依赖任何外部接口；生成的名字若被调用方忽略也不影响转存。
- 保留结构化信息：年份、分辨率、集数单独抽出，拼在片名后面，便于在盘里一眼分辨版本。
- 清洗噪声：【电影】类型标签、高清/中字等修饰词、文件系统非法字符（/ : * ? " < > |）全部去掉。
- 长度保护：云盘文件名通常 ≤ 255 字节，中文按 UTF-8 3 字节计，超长则截断到安全线。
"""

import re

# 文件系统非法字符（Windows / 多数网盘通用）
_ILLEGAL = re.compile(r'[\\/:*?"<>|]')
# 开头类型标签：【电影】 / [剧集] / （电影）/ 电影 流浪地球
_LEADING_TAG = re.compile(
    r"^\s*[\[【(（]?\s*"
    r"(电影|剧集|动漫|动画|纪录片|综艺|演唱会|短片|连续剧|网剧|电视剧|"
    r"美剧|韩剧|日剧|港剧|台剧|国产剧|欧美剧|日韩剧)\s*[\]】)］]?\s*"
)
_YEAR = re.compile(r"(?:19|20)\d{2}")
_RES = re.compile(r"(2160p|1440p|1080p|1080i|720p|480p|360p|4k|8k)", re.I)
_SEASON = re.compile(r"第\s*(\d{1,3})\s*季")
_EPISODE = re.compile(r"第\s*(\d{1,3})\s*[集话話期]")
# 需剔除的噪声修饰词（含编码/封装/语言/进度等，大小写不敏感）
_NOISE_WORDS = re.compile(
    r"(高清|中字|中英字幕|双语|国语|普通话|日语|韩语|英语|法语|泰语|粤语|台语|简体|繁体|字幕|无字幕|"
    r"未删减|加长版|导演剪辑版|终极版|完整版|无水印|官方|预告|花絮|合集|特别篇|番外|修复版|"
    r"国漫|动漫|经典|独家|首发|限时|免费|会员|迅雷|百度|夸克|阿里|网盘|磁力|种子|下载|资源|"
    r"更新至|连载中|"
    r"web[- ]?dl|webrip|bdrip|brrip|remux|hdtv|hdrip|dvdrip|h264|h265|x264|x265|hevc|avc|yuv420p|10bit|8bit)",
    re.I,
)
_KEEP = re.compile(r"[一-鿿A-Za-z0-9]")


def build_cn_filename(title: str, ext: str = "") -> str:
    """从 Telegram 标题构造干净的云盘文件名。

    例：
      "【电影】黑夜告白 2026 2160p 高清中字"  ->  "黑夜告白.2026.2160p.mp4"
      "Oppenheimer 2023 4K WEB-DL"           ->  "Oppenheimer.2023.4K.mkv"
    """
    if not title:
        return ""
    s = title.strip()
    s = _LEADING_TAG.sub("", s)

    year = _YEAR.search(s)
    res = _RES.search(s)
    season = _SEASON.search(s)
    episode = _EPISODE.search(s)

    # 去掉结构化信息与噪声，保留片名主体
    core = s
    core = _YEAR.sub("", core)
    core = _RES.sub("", core)
    core = _SEASON.sub("", core)
    core = _EPISODE.sub("", core)
    core = re.sub(r"[\[【】()（）\.\-_~]", " ", core)
    core = _NOISE_WORDS.sub("", core)
    core = _ILLEGAL.sub("", core)
    core = "".join(_KEEP.findall(core)).strip()
    if not core:
        core = "影视资源"

    parts = [core]
    if year:
        parts.append(year.group())
    if res:
        parts.append(res.group().upper())
    if season and episode:
        parts.append(f"{season.group(1)}季{episode.group(1)}集")
    elif episode:
        parts.append(f"{episode.group(1)}集")

    name = ".".join(p for p in parts if p)
    if ext:
        if not ext.startswith("."):
            ext = "." + ext
        name += ext

    # 超长截断（中文 UTF-8 3 字节，安全线 240 字节）
    if len(name.encode("utf-8")) > 240:
        name = (core[:60]) + (ext or "")
    return name


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
