"""资源身份识别：从一条标题里提炼「是谁 + 播到哪」，产出确定性的文件夹名与账本键。

背景：同一部影视常被不同频道用不同写法重复推送（换磁力 hash、换措辞、追剧加更）。
旧方案在「两段人类自由文本」之间做启发式模糊比对（names_match），规则越补越长。
本模块改为第一步就把标题解析成结构化身份：

    输入: 【电影】流浪地球2 The Wandering Earth II 2023 4K HDR 国语中字
    输出: core=流浪地球2  year=2023  sig=  folder=流浪地球2.2023  key=liulangdiqiu22023

    输入: 庆余年 第二季 第6集 1080p 中字
    输出: core=庆余年  year=0  sig=s02e06  folder=庆余年.S02E06  key=qingyunians02e06

    输入: 狂飙 更新至第15集（多集打包推送）
    输出: core=狂飙  year=2023  sig=  is_pack=True  folder=狂飙.2023  key=kuangbiao2023

设计原则：
- 确定性：同内容的不同写法，folder / key 必须收敛到同一个值（重复转存的判据）；
- 集数进文件夹名：同剧不同集（第5集/第6集/换季）得到不同 folder/key → 追剧不互相误杀；
- 电影 / 多集包：无集号资源 key 带年份，年份不同（翻拍重制）不算同一部；
- 面向机器而非排版：不追求名字绝对好看，追求「稳定 + 可分辨 + 与账本一致」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------- 标题里要剥离的噪声 ----------------

# 开头的类型标签：【电影】 / [剧集] / （电影）/ 电影 流浪地球
_LEADING_TAG = re.compile(
    r"^\s*[\[【(（]?\s*"
    r"(电影|剧集|动漫|动画|纪录片|综艺|演唱会|短片|连续剧|网剧|电视剧|"
    r"美剧|韩剧|日剧|港剧|台剧|国产剧|欧美剧|日韩剧)\s*[\]】)］]?\s*"
)

# 自动下载器 / 压制组长文件名的结构性尾巴（与 dedup.title_core 对齐）
_TAG_BRACE = re.compile(r"\{[^{}]*\}")                          # {tv tmdb-73982}
_EPISODE_RANGE = re.compile(r"(?i)\bs\d{1,2}\s*[-–—~]\s*s?\d{1,2}\b")  # S01-S02
_NUM_BRACKET = re.compile(r"\[\s*\d+(?:\.\d+)?\s*\]")           # [2.0]
_SIZE_BLOCK = re.compile(r"\(\s*\d+(?:\.\d+)?\s*(?:[kmgt]i?b)?[^()]{0,40}\)", re.I)  # (67.7GB 61个文件)
_SIZE_TOKEN = re.compile(r"(?i)\b\d+(?:\.\d+)?\s*[kmgt]i?b\b")  # 67.7GB
_FILE_COUNT = re.compile(r"\d+\s*个(?:文件|视频|资源)")          # 61个文件

# 年份（19xx / 20xx，四位数；注意别把 1080/2160 等分辨率误当成年份）
_YEAR = re.compile(r"(?<![\d.])(?:19|20)\d{2}(?![\d.])")

# 分辨率 / 编码 / 封装 / 画质音轨（剥掉，不参与身份）
_TECH = re.compile(
    r"(?i)"
    r"\b(2160p|1440p|1080p|1080i|720p|480p|360p|4k|8k|uhd|fhd|hd|sdr)\b"
    r"|\b(blu[- ]?ray|bluray|bdrip|brrip|web[- ]?dl|webrip|webdl|remux|hdtv|hdrip|"
    r"dvdrip|dvdr|h264|h265|x264|x265|hevc|avc|mpeg|yuv420p)\b"
    r"|\b(remastered|restored|imax|hdr10?\+?|dolby\s*(?:vision|atmos|truehd)|dovi|dv|"
    r"truehd|dts[- ]?hd|dts|x|ac3|aac|flac|lpcm|5\.1|7\.1|10bit|8bit)\b"
    r"|\.(mkv|mp4|avi|ts|rmvb|rm|iso|mov|wmv|flv|m2ts)(?=\s|$)"
)

# 中文噪声修饰词（语言 / 字幕 / 版本 / 广告词）
_NOISE_WORDS = re.compile(
    r"(高清|超清|中字|中英字幕|双语|国语|普通话|日语|韩语|英语|法语|泰语|粤语|台语|"
    r"简体|繁体|简中|繁中|字幕|无字幕|国配|中英|内嵌|外挂|"
    r"未删减|加长版|导演剪辑版|终极版|完整版|无水印|官方|预告|花絮|合集|特别篇|番外|"
    r"修复版|重制版|国漫|动漫|经典|独家|首发|限时|免费|会员|"
    r"迅雷|百度|夸克|阿里|网盘|磁力|种子|下载|资源|高清修复|"
    r"web[- ]?dl|webrip|bdrip|brrip|remux|hdtv|hdrip|dvdrip|bluray|blu[- ]?ray|"
    r"h264|h265|x264|x265|hevc|avc|yuv420p|10bit|8bit)",
    re.I,
)

# 剧集/综艺进度词（把「追更到哪了」从片名里摘走；含 1-5集 / 1~5集 / 1至5集 范围写法）
_PROGRESS = re.compile(
    r"更新至[^，。|]*(?:集|话|話|期)|更新到[^，。|]*(?:集|话|話|期)|连载中|连更中|大结局|完结|"
    r"全\s*\d+\s*[集话話]|共\s*\d+\s*[集话話]|\d{1,3}\s*[-~至]\s*\d{1,3}\s*[集话話期]"
)

# ---------------- 集数 / 季数解析（含中文数字） ----------------

_DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 中文/阿拉伯数字 token（季号、集号），支持到 99
_NUM_TOKEN = re.compile(r"[0-9一二两三四五六七八九十]+")

# 单集/季 模式（按优先级）
_SXXEXX = re.compile(r"(?i)\bs(\d{1,2})\s*[-_. ]?\s*e(\d{1,3})\b")           # S01E06 / s2-e3
_SEASON_FULL_CN = re.compile(r"第\s*([0-9一二两三四五六七八九十]+)\s*季")      # 第X季
_SEASON_FULL_EN = re.compile(r"(?i)\bseason\s*(\d{1,2})\b")                   # Season 2
_EP_CN = re.compile(r"第\s*([0-9一二两三四五六七八九十]+)\s*[集话話期]")        # 第X集/话/期
_EP_CN_BARE = re.compile(r"(?<![\d.])(\d{1,3})\s*[集话話期](?![\d.])")         # 18集（兜底）
_EP_EN = re.compile(r"(?i)\bep\.?\s*(\d{1,3})\b")                              # EP06

# 多集打包 / 聚合标记（出现即视为「包」，不生成单集签名，避免整包误杀后续单集）
_PACK = re.compile(
    r"全集|全\s*\d+\s*[集话話]|共\s*\d+\s*[集话話]|更新至|更新到|连载|连更|合集|"
    r"\d{1,3}\s*[-~至]\s*\d{1,3}\s*[集话話]|"                                  # 1-5集 / 1~5集 / 1至5集
    r"s\d{1,2}\s*[-~]\s*s?\d{1,2}\b",                                          # S01-S02
    re.I,
)

# 片名清洗：只保留中英文与数字
_KEEP = re.compile(r"[一-鿿A-Za-z0-9]")
# 分隔符（点、空格、括号、横线等都算），清洗时换成空格再拼
_SEP = re.compile(r"[。·•┈┄┉━─—\-–—_~|/\\\[\]【】()（）{}\"''`]+")


def _cn_int(tok: str):
    """中文/阿拉伯数字 token 转整数（支持 一~九十九）。失败返回 None。"""
    if not tok:
        return None
    if tok.isdigit():
        return int(tok)
    if "十" in tok:
        lo, _, hi = tok.partition("十")
        tens = _DIG.get(lo) if lo else 1
        ones = _DIG.get(hi, 0) if hi else 0
        if tens is None:
            return None
        return tens * 10 + ones
    if tok in _DIG:
        return _DIG[tok]
    # 连续多字（如 二十五 被单独传进来时）
    try:
        total = 0
        for c in tok:
            if c == "十":
                total = total * 10 if total else 10
            else:
                v = _DIG.get(c)
                if v is None:
                    return None
                total = total * 10 + v
        return total
    except Exception:
        return None


@dataclass
class ResourceInfo:
    """一条标题的结构化身份。

    字段语义：
    - core: 片名主体（保留原大小写与续集数字，如 流浪地球2 / Oppenheimer）
    - year: 解析到的年份；0 表示标题没写
    - sig: 单集/季 签名，s01e06 / s02（季包）；空串表示电影或多集打包资源
    - is_pack: 是否多集打包推送（含 全集/更新至/1-5集 等聚合标记）
    - folder: 落盘文件夹名（同内容的不同写法，此值一致）
    - key:    账本主键（folder 的小写字母数字形），云端/本地查重都用它
    """
    title: str = ""
    core: str = ""
    year: int = 0
    sig: str = ""
    is_pack: bool = False
    folder: str = ""
    key: str = ""


def _norm(s: str) -> str:
    """文件夹/标题归一化：小写、去掉全部非中英文数字字符。"""
    return "".join(_KEEP.findall((s or "").lower()))


def _strip_noise(title: str) -> str:
    """剥掉结构性尾巴与技术噪声，留下尽量干净的片名区。"""
    s = title
    s = _LEADING_TAG.sub("", s)
    s = _TAG_BRACE.sub(" ", s)
    s = _EPISODE_RANGE.sub(" ", s)
    s = _NUM_BRACKET.sub(" ", s)
    s = _SIZE_BLOCK.sub(" ", s)
    s = _TECH.sub(" ", s)
    s = _NOISE_WORDS.sub(" ", s)
    s = _SIZE_TOKEN.sub(" ", s)
    s = _FILE_COUNT.sub(" ", s)
    s = _YEAR.sub(" ", s)
    s = _SEP.sub(" ", s)
    s = _KEEP.findall(s)          # 再去掉空格标点等，只留字
    s = "".join(s).strip()
    # 去掉残留的纯数字段（体积 token、乱序数字），但保留中文数字连词
    s = re.sub(r"(?<![a-zA-Z一-鿿])[0-9]{2,}(?![a-zA-Z一-鿿0-9])", "", s).strip()
    return s


def analyze(title: str) -> ResourceInfo:
    """解析一条标题 → 结构化身份（文件夹名 + 账本键）。"""
    info = ResourceInfo(title=(title or "").strip())
    raw = info.title
    if not raw:
        return info

    # 年份
    m = _YEAR.search(raw)
    info.year = int(m.group()) if m else 0

    # 多集打包标记（先判，打包资源不生成单集签名）
    info.is_pack = bool(_PACK.search(raw))

    # 单集/季签名（仅非打包资源解析）
    if not info.is_pack:
        m = _SXXEXX.search(raw)
        if m:
            info.sig = f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}"
        else:
            season = None
            mc = _SEASON_FULL_CN.search(raw)
            if mc:
                season = _cn_int(mc.group(1))
            else:
                me = _SEASON_FULL_EN.search(raw)
                if me:
                    season = int(me.group(1))
            ep = None
            mec = _EP_CN.search(raw)
            if mec:
                ep = _cn_int(mec.group(1))
            else:
                mee = _EP_EN.search(raw)
                if mee:
                    ep = int(mee.group(1))
                else:
                    meb = _EP_CN_BARE.search(raw)
                    if meb:
                        ep = int(meb.group(1))
            if ep is not None:
                info.sig = f"s{(season or 1):02d}e{int(ep):02d}"
            elif season is not None:
                info.sig = f"s{season:02d}"   # 季包（Season 2 / 第2季，无集号）

    # 片名主体：先剥除年份与进度词，再剥技术噪声
    core_src = _PROGRESS.sub(" ", raw)
    core_src = _YEAR.sub(" ", core_src)
    # 单集中文写法（第X集/第X季第X集）要从片名里拿走，避免进 core
    core_src = _SEASON_FULL_CN.sub(" ", core_src)
    core_src = _EP_CN.sub(" ", core_src)
    # 英文单集/季标记（S01E06 / S02 / EP3 / Season 2）也别留在片名里
    if info.sig:
        core_src = re.sub(
            r"(?i)\bs\d{1,2}\s*[-_. ]?\s*e\d{1,3}\b"
            r"|\bseason\s*\d{1,2}\b|\bep\.?\s*\d{1,3}\b"
            r"|\bs\d{1,2}\b",
            " ", core_src)
    info.core = _strip_noise(core_src)
    # 中文片名为主时，丢掉夹带的英文（不同频道常写不同译名/写不写英文，会破坏账本一致性）
    if _KEEP.search(info.core) and re.search(r"[一-鿿]", info.core):
        info.core = re.sub(r"[a-zA-Z]", "", info.core)
        info.core = "".join(_KEEP.findall(info.core))

    # 组装文件夹名
    if info.sig:
        folder = f"{info.core}.{info.sig.upper()}" if info.core else info.sig.upper()
    else:
        folder = info.core
        if info.year:
            folder = f"{info.core}.{info.year}" if info.core else str(info.year)
    if not folder:
        folder = "影视资源"
    info.folder = folder
    info.key = _norm(folder)
    return info


def folder_name(title: str) -> str:
    """对外：给定标题返回落盘文件夹名。"""
    return analyze(title).folder


def norm(s: str) -> str:
    """对外：给定字符串返回账本比较用的归一化形式。"""
    return _norm(s)


if __name__ == "__main__":
    demos = [
        "【电影】黑夜告白 2026 2160p 高清中字",
        "Oppenheimer 2023 4K WEB-DL",
        "流浪地球2 2023 2160p 4K 完整版 未删减",
        "庆余年 第2季 第3集 1080p 中字",
        "庆余年 第06集 1080p 国语中字",
        "庆余年 S01E06 2160p",
        "狂飙 更新至第15集 1080P 国语中字",
        "狂飙 1-5集 高清",
        "白夜追凶 (2017){tv tmdb-73982}[S01-S02][2160p][HEVC][AAC][中字][2.0](67.7GB 61个文件)",
        "HeiYeGaoBai.2026.2160p.WEB-DL",
        "凡人修仙传 第三季 第10话",
        "奥本海默 Oppenheimer 2023 BluRay 英语中字",
    ]
    for t in demos:
        a = analyze(t)
        print(f"  {t[:46]:<46} core={a.core:<10} y={a.year:<5} sig={a.sig or '-':<8} "
              f"pack={int(a.is_pack)} folder={a.folder}")
