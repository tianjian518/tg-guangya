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
- 英文片名翻译：先查本地字典 → 再查 TMDB API（config.tmdb.api_key 配了才生效）→ 都拿不到才保留原名。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

try:
    from core import media_meta
except ImportError:  # 直接 python3 core/ident.py 跑自带 demo 时没有包上下文
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import media_meta

# ---------------- 标题里要剥离的噪声 ----------------

# 开头的类型标签：【电影】 / [剧集] / （电影）/ 电影 流浪地球 / 欧美电影: xxx
# 注意：修饰词（最新/高清/4K 等）与地区词（欧美/华语/国产 等）均为可选，
# 且「欧美电影」这类整体词必须列在「电影」之前（长的优先）。
_LEADING_TAG = re.compile(
    r"^\s*[\[【(（]?\s*"
    r"(?:最新|热门|经典|精品|推荐|高清|超清|蓝光|原盘|4k|8k|uhd|1080p|720p|2160p)?\s*"
    r"(?:欧美电影|华语电影|国产电影|外语电影|亚洲电影|日本电影|韩国电影|港台电影|"
    r"电影|剧集|动漫|动画|纪录片|综艺|演唱会|短片|连续剧|网剧|电视剧|"
    r"美剧|韩剧|日剧|港剧|台剧|国产剧|欧美剧|日韩剧|港片|日影|韩影)"
    r"\s*[\]】)）］]?\s*[:：|｜\-–—]?\s*",
    re.I,
)

# 自动下载器 / 压制组长文件名的结构性尾巴（与 dedup.title_core 对齐）
_TAG_BRACE = re.compile(r"\{[^{}]*\}")                          # {tv tmdb-73982}
_EPISODE_RANGE = re.compile(r"(?i)\bs\d{1,2}\s*[-–—~]\s*s?\d{1,2}\b")  # S01-S02
_NUM_BRACKET = re.compile(r"\[\s*\d+(?:\.\d+)?\s*\]")           # [2.0]
_SIZE_BLOCK = re.compile(r"\(\s*\d+(?:\.\d+)?\s*(?:[kmgt]i?b)?[^()]{0,40}\)", re.I)  # (67.7GB 61个文件)
_SIZE_TOKEN = re.compile(r"(?i)\b\d+(?:\.\d+)?\s*[kmgt]i?b\b")  # 67.7GB
_FILE_COUNT = re.compile(r"\d+\s*个(?:文件|视频|资源)")          # 61个文件

# 年份（19xx / 20xx，四位数）。
# ⚠️ 旧实现用一条正则硬啃，同时要防版本号（1.2023）又要防分辨率（2160p），
#    结果两个都不能兼得：负向后顾 (?<!\d\.) 把 "Ne.Zha.2.2025" 里的 2025 也一起排除了
#    （前面是 "2."，看着和版本号一模一样），year=0 → 落盘名变成「NeZha」而不是「哪吒之魔童闹海.2025」。
# 改成：先把所有四位数候选捞出来，再按值过滤掉分辨率。版本号那种写法极罕见，
# 就算命中也只是把版本号当年份，代价远小于年份整体丢失。
_NOT_YEAR = {"1080", "1440", "1920", "2048", "2160", "4320", "1600", "1280", "1024"}
_YEAR_ANY = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _find_year(s: str) -> int:
    """从文本里取年份；分辨率（1080 / 2160 …）不算。取不到返回 0。"""
    for m in _YEAR_ANY.finditer(s or ""):
        if m.group() in _NOT_YEAR:
            continue
        return int(m.group())
    return 0


def _strip_year(s: str) -> tuple[str, int]:
    """剥掉年份，返回 (剩余文本, 年份)。用于把年份从片名区里拿走。"""
    for m in _YEAR_ANY.finditer(s or ""):
        if m.group() in _NOT_YEAR:
            continue
        return (s[:m.start()] + " " + s[m.end():]), int(m.group())
    return (s or ""), 0

# 分辨率 / 编码 / 封装 / 画质音轨（剥掉，不参与身份）
_TECH = re.compile(
    r"(?i)"
    r"\b(2160p|1440p|1080p|1080i|720p|480p|360p|4k|8k|uhd|fhd|hd|sdr)\b"
    r"|\b(blu[- ]?ray|bluray|bdrip|brrip|web[- ]?dl|webrip|webdl|remux|hdtv|hdrip|"
    r"dvdrip|dvdr|h264|h265|x264|x265|hevc|avc|mpeg|yuv420p)\b"
    r"|\b(remastered|restored|imax|hdr(?:10)?\+?|dolby\s*(?:vision|atmos|truehd)|dovi|dv|"
    r"truehd|ac3|aac|flac|lpcm|5\.1|7\.1|10bit|8bit)\b"
    # DTS 家族整体匹配（含可选 MA 后缀）。必须放在所有短分支之前：
    # 若先被 dts[- ]?hd 吃掉，就会剩下孤立的 MA 粘进片名。
    # 且绝不能退化成裸 ma —— 那会啃掉 Terminator/Batman/The Matrix 里的 "ma"。
    # 分隔符要含点号：压制组常写 DTS-HD.MA.5.1（点号分隔），
    # 只认 [- ] 会剩下孤立的 MA 粘进片名（Dune.2021…DTS-HD.MA → DuneMA）。
    r"|\bdts(?:[-. ]?(?:x|hd(?:[-. ]?ma)?|ma))?\b"
    # DDP5.1 / DD+5.1（杜比数字Plus）漏了的话会粘进片名：
    # "YOLO.2024...DDP5.1" 曾解析成 "YOLODDP51"，字典自然查不到译名
    r"|dd5\.1|dts5\.1|ddp\s*5\.1|dd\+?\s*5\.1|\bddp\b|\bdd\+\b|eac3|e-\s*ac3|\batmos\b|\bmlp\b"
    r"|\.(mkv|mp4|avi|ts|rmvb|rm|iso|mov|wmv|flv|m2ts)(?=\s|$)"
)

# 中文噪声修饰词（语言 / 字幕 / 版本 / 广告词）
_NOISE_WORDS = re.compile(
    r"(高清|超清|中字|中英字幕|双语|国语|普通话|日语|韩语|英语|法语|泰语|粤语|台语|"
    r"简体|繁体|简中|繁中|字幕|无字幕|国配|中英|内嵌|外挂|"
    # 音轨/声轨要整体匹配：旧表只写了「双音」，"双音轨" 会剩一个「轨」字粘进片名
    # （实测「热辣滚烫 … 双音轨」被命名成「热辣滚烫轨.2024」）
    r"未删减|加长版|导演剪辑版|终极版|完整版|无水印|官方|预告|花絮|合集|特别篇|番外|"
    r"音轨|声轨|多音轨|双音轨|国粤双语|硬字幕|软字幕|特效字幕|内封字幕|"
    r"修复版|重制版|国漫|动漫|经典|独家|首发|限时|免费|会员|"
    r"迅雷|百度|夸克|阿里|网盘|磁力|种子|下载|资源|高清修复|"
    r"原版|双音|原音|原声|国语|韩语|日语|英语|配音|Dubbed|DUBBED|Lion|Mandarin|"
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
_SEASON_BARE = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,2})(?![a-z0-9])")          # S03（季包，无集号）
_EP_CN = re.compile(r"第\s*([0-9一二两三四五六七八九十]+)\s*[集话話期]")        # 第X集/话/期
_EP_CN_BARE = re.compile(r"(?<![\d.])(\d{1,3})\s*[集话話期](?![\d.])")         # 18集（兜底）
_EP_EN = re.compile(r"(?i)\bep\.?\s*(\d{1,3})\b")                              # EP06

# 多集打包 / 聚合标记（出现即视为「包」，不生成单集签名，避免整包误杀后续单集）
_PACK = re.compile(
    r"全集|全\s*\d+\s*[集话話]|共\s*\d+\s*[集话話]|更新至|更新到|连载|连更|合集|"
    r"\d{1,3}\s*[-~至]\s*\d{1,3}\s*[集话話]|"                                  # 1-5集 / 1~5集 / 1至5集
    r"s\d{1,2}\s*[-~]\s*s?\d{1,2}\b|"                                          # S01-S02
    r"\bpart\s*\d+\s*(&|and|与)\s*(?:part\s*)?\d+\b",                       # Part 1 & 2 / Part I & II
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
    - region_hint: 地区提示（由 core 语言特征自动推断），供分类器优先使用。
                   非空时分类器直接采信，绕过原始标题的语言推断规则。
    """
    title: str = ""
    core: str = ""
    year: int = 0
    sig: str = ""
    is_pack: bool = False
    folder: str = ""
    key: str = ""
    region_hint: str = ""   # "cn" / "jpkr" / "west" / ""（空=无提示）
    region_hint_strong: bool = False  # True=来自片名库/TMDB（权威），False=原标题语言推断


def _norm(s: str) -> str:
    """文件夹/标题归一化：小写、去掉全部非中英文数字字符。"""
    return "".join(_KEEP.findall((s or "").lower()))


def _strip_noise(title: str, *, strip_trailing_nums: bool = False) -> str:
    """剥掉结构性尾巴与技术噪声，留下尽量干净的片名区。
    strip_trailing_nums=True 时额外去掉末尾的纯数字段（用于英文 core 翻译前的清理）。"""
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
    s, _y = _strip_year(s)
    s = _SEP.sub(" ", s)
    s = _KEEP.findall(s)          # 再去掉空格标点等，只留字
    s = "".join(s).strip()
    if strip_trailing_nums:
        # 只去掉末尾 ≥3 位纯数字（"RandomMovie999"→"RandomMovie"），放过单/双位数（"JohnWick4"→不变）
        s = re.sub(r"[0-9]{3,}$", "", s).strip()
    return s


# ---------------- 英文片名 → 中文译名 ----------------
# 查表逻辑统一收口到 core/media_meta.py：
#   内置字典（680 条，已剔除旧表里 "thewho": "谁" 那类水货）
#   → 用户自定义字典（数据目录 user_dict.json，可自己加）
#   → TMDB（movie + tv 都查，还能带出 original_language 判地区）
#   → 两级缓存（进程内 + 数据目录 media_cache.json）
# 这里只做薄封装，把查到的「译名 + 地区」一起带出去给分类器用。

# 查字典前要先摘掉的集数/分部标记（"BreakingBadS01" / "Part1&2" 这类粘连写法）
_EN_SIG = re.compile(r"(?i)\b(?:s\d{1,2}e\d{1,2}|ep\s*\d+)\b")
_EN_SEASON_TAIL = re.compile(r"(?i)(?<=[a-zA-Z])s\d{1,2}(?=$|[^a-zA-Z0-9])")
_EN_PART_RANGE = re.compile(r"(?i)\bpart\s*\d+\s*(?:&|and|与)\s*(?:part\s*)?\d+\b")
_EN_PART = re.compile(r"(?i)part\d+")


def _lookup_en(core: str, year: int = 0) -> tuple[str, str]:
    """查英文片名的中文译名与地区码，返回 (译名, 地区码)。

    异常一律吞掉：翻译不出来最多保留英文原名，绝不能中断一条标题的解析
    ——本函数在 analyze() 的兜底路径上，抛异常会连带整轮转存失败。
    """
    if not core:
        return "", ""
    try:
        q = _EN_SIG.sub(" ", core)
        q = _EN_SEASON_TAIL.sub("", q)
        q = _EN_PART_RANGE.sub(" ", q)
        q = _EN_PART.sub("", q).strip()
        if not q:
            q = core
        meta = media_meta.lookup(q, year)
        return meta.cn_name, meta.region
    except Exception:
        return "", ""


def analyze(title: str) -> ResourceInfo:
    """解析一条标题 → 结构化身份（文件夹名 + 账本键）。"""
    info = ResourceInfo(title=(title or "").strip())
    raw = info.title
    if not raw:
        return info

    # 年份（分辨率 1080/2160 不算，见 _find_year）
    info.year = _find_year(raw)

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
                else:
                    msb = _SEASON_BARE.search(raw)
                    if msb:
                        season = int(msb.group(1))
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
    core_src, _ = _strip_year(core_src)
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
    translated_from_en = False
    meta_region = ""
    if not re.search(r"[一-鿿]", info.core or ""):
        info.core = _strip_noise(info.core, strip_trailing_nums=True)
        translated, meta_region = _lookup_en(info.core, info.year)
        if translated:
            info.core = translated
            translated_from_en = True
    # 中文片名为主时，丢掉夹带的英文（不同频道常写不同译名/写不写英文，会破坏账本一致性）
    core_en = ""
    if _KEEP.search(info.core) and re.search(r"[一-鿿]", info.core):
        core_en = "".join(re.findall(r"[A-Za-z0-9]+", info.core))
        info.core = re.sub(r"[a-zA-Z]", "", info.core)
        info.core = "".join(_KEEP.findall(info.core))
    # 「中文译名 + 英文原名」的标题（"泰坦尼克号 Titanic 1997" / "阿凡达 Avatar"）：
    # 中文 core 查不出地区，再拿夹带的英文原名查一次，只取地区、不动中文片名。
    # 少了这一步，这类标题只能靠「原文有中文」判华语，好莱坞片会被分进华语电影。
    if not meta_region and core_en and not translated_from_en:
        _, meta_region = _lookup_en(core_en, info.year)
    # 地区提示。两条来源，前者优先：
    #   ① 译名来源自带的地区（本地字典登记表 / TMDB 的 original_language）——
    #      这是纯英文标题（压制组命名）唯一的地区线索，没有它
    #      "The.Wandering.Earth.II.2023" 只能靠「没中文→欧美」兜底，必然分错；
    #   ② 原标题本身是中文 → 华语（港片/华语片常见写法）。
    # 经翻译变成中文的（The Matrix→黑客帝国）不算，否则好莱坞片会误判华语。
    if meta_region:
        info.region_hint = meta_region
        info.region_hint_strong = True
    elif re.search(r"[一-鿿]", raw) and not translated_from_en:
        info.region_hint = "cn"
        info.region_hint_strong = False
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
