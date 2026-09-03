"""两级去重：本地记录 + 云端复查。

为什么需要云端复查？
- 一级（本地 hash 去重）只在「同一磁力链接」再次出现时有效。但同一部影视常被不同
  频道以不同磁力（不同 btih）多次发布，hash 不同 → 一级去重失效，会被重复转存。
- 光鸭网盘没有全局搜索接口，只能「在分类目录下列举文件、按片名匹配」来复查。

决策流程（贴合用户诉求）：
  1. 新链接出现 → 先看本地转存记录
       - 从没转过（本地无记录）     → 进第 2 步云端复查
       - 转过（本地有记录）         → 进第 2 步云端复查
  2. 云端复查：在资源对应的分类目录下，按片名匹配是否已存在
       - 云端已有同名资源           → 丢弃（用户保留着 / 不同磁力同一片）
       - 云端没有（被用户删了）     → 重新转存一次

追剧 / 追番场景：同一部剧会分多集陆续发布（第1集、第2集……第N集），每一集都是
不同的磁力链接（不同 btih）。
  - 不同集必须用集数签名区分：第6集不能被第5集误杀，应当照常转存进同一分类目录。
  - 同一集被不同磁力重复发布（第5集换了个链接）：仍按「同名同集」去重丢弃。
实现见 episode_sig / names_match 中的集数感知规则。
"""
from __future__ import annotations

import logging
import re
import threading
import time

log = logging.getLogger(__name__)

# ---------------- 片名归一化（提取「核心片名」用于匹配）----------------

# 这些是「噪声后缀」，匹配时应当剥离，只保留可识别的片名主体
_NOISE = re.compile(
    r"(?i)"
    r"\b(19|20)\d{2}\b"                                  # 年份
    r"|\b(2160p|1080p|1080i|720p|480p|360p|4k|8k)\b"     # 分辨率
    r"|\b(blu[- ]?ray|bluray|bdrip|brrip|web[- ]?dl|webrip|webdl|remux|hdtv|hdrip|dvdrip|dvdr|h264|h265|x264|x265|hevc|avc|10bit|8bit|yuv420p|mp4|mkv)\b"  # 编码/封装
    r"|\b(hdr|hdr10|hdr10\+|dolby|dovi|dv|imax|atmos|truehd|dts|ac3|aac|flac|5\.1|7\.1)\b"  # 画质/音轨
    r"|第\s*\d{1,3}\s*[集话話期]|全\d+集|更新至|更新到|连载中|共\d+集"   # 剧集/综艺进度
    r"|\bs\d{1,2}e\d{1,3}\b|\bep?\d{1,3}\b|\bseason\s*\d+\b|第\s*\d{1,3}\s*季"           # S01E02 / EP03 / 第X季
    r"|(国语|普通话|中字|中英字幕|双语|日语|韩语|英语|法语|泰语|印地语|粤语|台语|简体|繁体|字幕|无字幕)"
    r"|(修复版|未删减|加长版|导演剪辑版|终极版|完整版|高清|无水印|官方|预告|花絮|合集|特别篇|番外)"
    r"|第\s*\d{1,3}\s*[集话話期]|全\d+集|更新至[^集话話期]*[集话話期]|更新到[^集话話期]*[集话話期]|连载中|共\d+集|\d{1,3}\s*[集话話期]"
    r"|[\[\]【】()（）\.\-_~]"
)

_CJK = re.compile(r"[一-鿿]")
_ALNUM = re.compile(r"[a-z0-9一-鿿]")

# 标题开头的类型标签：如【电影】 / [剧集] / （电影）/ 电影  / 剧集 流浪地球
_LEADING_TAG = re.compile(r"^\s*[\[【(（]?\s*(电影|剧集|动漫|动画|纪录片|综艺|演唱会|短片|连续剧|网剧|电视剧|美剧|韩剧|日剧|港剧|台剧|国产剧|欧美剧|日韩剧)\s*[\]】)］]?\s*")


def title_core(title: str) -> str:
    """从标题/文件名里提取用于匹配的核心片名（小写、去噪声、去标点）。"""
    if not title:
        return ""
    s = _LEADING_TAG.sub("", title)   # 先去掉开头的【电影】这类类型标签
    s = _NOISE.sub(" ", s)
    s = s.lower()
    # 只保留中文与字母数字，去掉所有空格/标点
    s = "".join(_ALNUM.findall(s))
    # 去掉可能残留的纯数字段（如体积 1.2g、集数）干扰
    s = re.sub(r"\d+g\b", "", s)
    return s


def _is_cjk(s: str) -> bool:
    return bool(s) and bool(_CJK.search(s))


# 续集/版本号这类「多出来的尾巴」应视为不同片子：含数字或中文即不算匹配
_REMAINDER_BAD = re.compile(r"[0-9a-zA-Z一-鿿]")
# 中文核：保留中文与数字（数字用于区分续集，如 流浪地球2）
_CJK_CORE = re.compile(r"[^一-鿿0-9]")
# 英文核：只保留字母数字
_LATIN_CORE = re.compile(r"[^a-z0-9]")


def _cjk_core(s: str) -> str:
    return _CJK_CORE.sub("", s)


def _latin_core(s: str) -> str:
    return _LATIN_CORE.sub("", s)


# ---------------- 集数签名（追剧 / 追番）----------------
# 把「第X集 / S01E02 / EP03 / 第X期 / 第X季第X集」归一化成统一的 "s01e02" 形式，
# 用于区分同一部剧的不同集——这是「第6集不能被第5集误杀」的关键。
_SEASON = re.compile(r"第\s*(\d{1,3})\s*季", re.I)
_EPISODE = re.compile(r"第\s*(\d{1,3})\s*[集话話期]", re.I)
_SEASON_EPISODE = re.compile(r"s(\d{1,2})\s*[-_.]?\s*e(\d{1,3})", re.I)
_EP = re.compile(r"\bep?\s*(\d{1,3})\b", re.I)


def episode_sig(title: str) -> str | None:
    """提取集数签名；无集数信息（电影 / 整剧包「全30集」）返回 None。

    归一化：
      - S01E02 / s1e2          → "s01e02"
      - 第3集 / 第03话 / 第5期   → "s01e03"（未标季默认第1季）
      - EP03 / E03             → "s01e03"
      - 第2季第3集              → "s02e03"
    """
    if not title:
        return None
    s = title.lower()
    m = _SEASON_EPISODE.search(s)
    if m:
        return f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}"
    season = 1
    ms = _SEASON.search(s)
    if ms:
        season = int(ms.group(1))
    me = _EPISODE.search(s)
    if me:
        return f"s{season:02d}e{int(me.group(1)):02d}"
    mep = _EP.search(s)
    if mep:
        return f"s{season:02d}e{int(mep.group(1)):02d}"
    return None


# ---------------- 质量评分（洗版 / 版本升级）----------------
# 给一条资源标题打「质量分」，分越高代表版本越好。洗版时用来判断「新链接是否比盘里已有的更好」。
_RES_SCORE = [
    (2160, 120, re.compile(r"2160p|4k|uhd", re.I)),
    (1440, 90, re.compile(r"1440p", re.I)),
    (1080, 60, re.compile(r"1080p|fhd", re.I)),
    (720, 35, re.compile(r"720p|\bhd\b", re.I)),
    (480, 15, re.compile(r"480p", re.I)),
]
_CODEC_SCORE = re.compile(r"\b(hevc|h265|x265|av1|remux|bdrip|web[- ]?dl)\b", re.I)
_AUDIO_SCORE = re.compile(r"\b(atmos|dts[- ]?hd|truehd|dolby|dts|flac|lpcm)\b", re.I)
_HDR_SCORE = re.compile(r"\b(hdr|hdr10|hdr10\+|dolby\s*vision|\bdv\b)\b", re.I)
_BIT_SCORE = re.compile(r"\b10[- ]?bit\b", re.I)


def quality_score(title: str) -> int:
    """资源标题的质量分（越大越好）。用于洗版时判断「要不要替换盘里已有的旧版本」。

    评分维度：分辨率（主导，4K=120 / 1080P=60 / 720P=35）+
    编码（HEVC/REMUX +15）+ 音轨（Atmos/DTS-HD/TrueHD +10）+ HDR/DV（+8）+ 10bit（+4）。
    """
    if not title:
        return 0
    s = title
    score = 0
    for _lvl, pts, pat in _RES_SCORE:
        if pat.search(s):
            score = max(score, pts)
    if _CODEC_SCORE.search(s):
        score += 15
    if _AUDIO_SCORE.search(s):
        score += 10
    if _HDR_SCORE.search(s):
        score += 8
    if _BIT_SCORE.search(s):
        score += 4
    return score


def _clean_containment(a: str, b: str) -> bool:
    """a、b 是否指向同一片子（核心名相等，或包含关系且尾巴只是分隔符）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = sorted([a, b], key=len)
    if short not in long:
        return False
    min_len = 2 if _is_cjk(short) else 4
    if len(short) < min_len:
        return False
    # 多出来的尾巴若含数字/字母/中文（如续集号、版本号），视为不同片子
    tail = long[len(short):]
    return not _REMAINDER_BAD.search(tail)


def names_match(resource_title: str, existing_name: str) -> bool:
    """判断云端已有文件名 existing_name 是否与本资源 resource_title 指向同一部片子。

    三套核心核依次匹配，兼顾：
    - 同语言同片（中文核/英文核精确相等，含续集数字）
    - 中英混合标题（一侧只有中文、另一侧只有英文时仍能命中）
    - 续集区分（流浪地球 vs 流浪地球2 不会误判为同一部）
    - 追剧/追番（第6集不被第5集误杀）：两方都带集数签名且不同时，判为不同资源。
    """
    a = title_core(resource_title)
    b = title_core(existing_name)
    if not a or not b:
        return False
    # 追剧/追番关键规则：两方都带集数签名（第X集 / SxxExx / EPxx）且**不同**时，
    # 视为不同集（第6集不应被第5集误杀）。只有当集数签名一致（或有一方不标集数，
    # 如整剧包「全30集」、未单独标集的电影）时，才继续用核心片名比对。
    ea, eb = episode_sig(resource_title), episode_sig(existing_name)
    if ea and eb and ea != eb:
        return False
    if _clean_containment(a, b):
        return True
    # 混合语言：分别在「中文核（保留数字）」「英文核」上做二次匹配
    ca, cb = _cjk_core(a), _cjk_core(b)
    if ca and cb and _clean_containment(ca, cb):
        return True
    la, lb = _latin_core(a), _latin_core(b)
    if la and lb and len(la) >= 3 and la == lb:
        return True
    return False


from dataclasses import dataclass


@dataclass
class DedupDecision:
    action: str          # transfer / skip_exists / retransfer / upgrade
    reason: str
    category: str
    parent_id: str = ""
    replace_file_id: str = ""    # 洗版：待删除的旧版本 fileId
    replace_parent_id: str = ""  # 洗版：旧版本所在目录 fileId


class CloudDedup:
    """两级去重决策器。"""

    def __init__(self, client, resolver, classifier,
                 cloud_check_new: bool = True, cache_ttl: float = 300.0,
                 organize_enabled: bool = True, upgrade: bool = False) -> None:
        self.client = client
        self.resolver = resolver
        self.classifier = classifier
        self.cloud_check_new = cloud_check_new
        self.cache_ttl = cache_ttl
        self.organize_enabled = organize_enabled
        self.upgrade = upgrade
        self._lock = threading.Lock()
        # parent_id -> (timestamp, [条目列表])，条目含 file_id/name/size/res_type/parent_id
        self._dir_cache: dict[str, tuple[float, list[dict]]] = {}

    # ---------- 云端目录列表（带缓存，存完整条目）----------
    def _list_dir_entries(self, parent_id: str) -> list[dict]:
        now = time.time()
        with self._lock:
            hit = self._dir_cache.get(parent_id)
            if hit and now - hit[0] < self.cache_ttl:
                return hit[1]
        try:
            entries = self.client.list_dir(parent_id)
        except Exception as exc:
            log.warning("云端列举目录失败（%s），本次跳过云端查重", exc)
            return []
        for e in entries:
            e.setdefault("parent_id", parent_id)
        with self._lock:
            self._dir_cache[parent_id] = (now, entries)
        return entries

    def _list_dir_names(self, parent_id: str) -> list[str]:
        return [e["name"] for e in self._list_dir_entries(parent_id)
                if int(e.get("res_type", 0)) != 2]

    def _find_existing(self, category: str, title: str) -> dict | None:
        """在资源对应的分类目录（或根目录）里找「同名同集」的已有文件条目。

        返回该条目 dict（含 file_id / name / parent_id），找不到返回 None。
        集数感知由 names_match 保证：第6集不会命中第5集。
        """
        if category and self.resolver.exists(category):
            parent_id, _ = self.resolver.resolve(category, create_missing=False)
        else:
            parent_id = self.resolver.root_id
        if not parent_id:
            return None
        for e in self._list_dir_entries(parent_id):
            if int(e.get("res_type", 0)) == 2:
                continue
            if names_match(title, e["name"]):
                return e
        return None

    def cloud_has(self, category: str, title: str) -> bool:
        """资源对应的分类目录（或根目录）里是否已存在同名片子。

        传入原始标题（非剥离后的核心名），以便 names_match 内部做集数感知匹配。
        """
        return self._find_existing(category, title) is not None

    def decide(self, hash_: str, title: str, store) -> DedupDecision:
        """综合本地记录与云端核查，给出去重决策。

        store 需提供 get(hash_) -> 记录对象或 None，记录含 status 字段。
        """
        cat = self.classifier.classify(title).category if self.organize_enabled else ""
        rec = None
        try:
            rec = store.get(hash_)
        except Exception:
            rec = None
        local_done = rec is not None and (rec.status in ("done", "submitted"))

        if rec and local_done:
            # 第 2 步：本地说转过 → 复查云端
            existing = self._find_existing(cat, title)
            if existing:
                return self._decide_upgrade(existing, cat, title,
                                            "本地已转存且云端仍存在")
            return DedupDecision("retransfer",
                                "本地已转存但云端已无（用户删除过），重新转存", cat, "")

        # 本地无记录（新磁力）
        if self.cloud_check_new:
            existing = self._find_existing(cat, title)
            if existing:
                return self._decide_upgrade(existing, cat, title,
                                            "云端已存在同名资源（可能是不同磁力的同一片子）")
        return DedupDecision("transfer", "新资源，转存", cat, "")

    def _decide_upgrade(self, existing: dict, cat: str, title: str, base_reason: str) -> DedupDecision:
        """云端已有同名同集文件时的决策：洗版（新更好则替换）或丢弃。"""
        if self.upgrade:
            new_q = quality_score(title)
            old_q = quality_score(existing.get("name", ""))
            if new_q > old_q:
                return DedupDecision(
                    "upgrade",
                    f"{base_reason}；新版本质量更优（{new_q} > {old_q}），洗版替换旧版本",
                    cat, "",
                    replace_file_id=existing.get("file_id", ""),
                    replace_parent_id=existing.get("parent_id", ""),
                )
        return DedupDecision("skip_exists", f"{base_reason}，丢弃", cat, "")
