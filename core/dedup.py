"""三级去重：同磁力 → 内容账本 → 云端复查。

思路（v1.3.0 起重构，核心变化是「不再靠猜文本，先建账本」）：
  转存成功的同时，按 core/ident.py 的身份识别把「这部片/这集」记进本地 titles 账本。
  下次无论哪个频道、哪种写法、换没换磁力 hash，只要内容是同一份，账本先命中 → 不重复落盘。

决策流程：
  1. 同磁力 hash 已处理（本地 magnets 表）      → 跳过（最便宜，刷屏防重）
  2. 内容账本命中（titles 表，同一 folder key）→ 跳过（同片不同 hash / 不同写法重复推送）
  3. 云端复查（防「不是本程序转的」历史资源）：
       - 先精确：目录里存在同名 folder（我们自己落盘的标准名）→ 跳过/洗版
       - 再模糊：names_match 兜底识别 MoviePilot/老版本等历史遗留命名
  追剧/追番的保证：集数签名（SxxExx）进 folder key → 第6集永远不会被第5集顶掉；
  同一集被不同磁力重复发布 → 账本同 key 直接命中丢弃。

云端复查注意事项（老坑）：
- 目录列表带缓存，条目含文件与文件夹（文件夹是磁力落盘主要形态，早期只比文件导致
  副本泛滥）；
- names_match 只对「不带集号」的目录做无集数匹配：带单集签名的新资源，遇到不带集号的
  云端目录（可能是整包/整季）一律不判为同一集，宁可在首次重复时多转一份，绝不漏集。
"""
from __future__ import annotations

import logging
import re
import threading
import time

# 拼音兜底（可选依赖）：光鸭落盘名常是种子发布名（英文/拼音，如 HeiYeGaoBai.2026），
# 与频道中文标题《黑夜告白》对不上。装了 pypinyin 就能把中文侧拼音化再比对，
# 大幅降低「云端复查误判没有同名 → 反复复制副本」的概率；没装则静默跳过该层。
try:
    from pypinyin import lazy_pinyin

    _HAS_PINYIN = True
except Exception:  # noqa: BLE001 - 可选依赖缺失时优雅降级
    lazy_pinyin = None
    _HAS_PINYIN = False

from core.ident import analyze, norm as norm_name  # noqa: E402

log = logging.getLogger(__name__)

# ---------------- 片名归一化（提取「核心片名」用于模糊兜底匹配）----------------

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

# 自动下载器长文件名的结构性尾巴（title_core 里在 _NOISE 之前剥离）
_TAG_BRACE = re.compile(r"\{[^{}]*\}")                                        # {tv tmdb-73982}
_EPISODE_RANGE = re.compile(r"(?i)\bs\d{1,2}\s*[-–—~]\s*s?\d{1,2}\b")        # S01-S02（整剧包，非逐集）
_NUM_BRACKET = re.compile(r"\[\s*\d+(?:\.\d+)?\s*\]")                          # [2.0]
_SIZE_BLOCK = re.compile(r"\(\s*\d+(?:\.\d+)?\s*(?:[kmgt]i?b)?[^()]{0,40}\)", re.I)  # (67.7GB 61个文件)
_SIZE_TOKEN = re.compile(r"(?i)\b\d+(?:\.\d+)?\s*[kmgt]i?b\b")                # 67.7GB（无括号兜底）
_FILE_COUNT = re.compile(r"\d+\s*个(?:文件|视频|资源)")                         # 61个文件

_CJK = re.compile(r"[一-鿿]")
_ALNUM = re.compile(r"[a-z0-9一-鿿]")

# 标题开头的类型标签：如【电影】 / [剧集] / （电影）/ 电影  / 剧集 流浪地球
_LEADING_TAG = re.compile(r"^\s*[\[【(（]?\s*(电影|剧集|动漫|动画|纪录片|综艺|演唱会|短片|连续剧|网剧|电视剧|美剧|韩剧|日剧|港剧|台剧|国产剧|欧美剧|日韩剧)\s*[\]】)］]?\s*")


def title_core(title: str) -> str:
    """从标题/文件名里提取用于模糊兜底匹配的核心片名（小写、去噪声、去标点）。"""
    if not title:
        return ""
    s = _LEADING_TAG.sub("", title)   # 先去掉开头的【电影】这类类型标签
    s = _TAG_BRACE.sub(" ", s)         # {tv tmdb-73982} 整块剥（含内部字母数字）
    s = _EPISODE_RANGE.sub(" ", s)     # S01-S02 整剧包范围
    s = _NUM_BRACKET.sub(" ", s)       # [2.0] 纯数字方括号
    s = _SIZE_BLOCK.sub(" ", s)        # (67.7GB 61个文件) 体积括号块
    s = _NOISE.sub(" ", s)
    s = _SIZE_TOKEN.sub(" ", s)        # 无括号的体积 token（兜底）
    s = _FILE_COUNT.sub(" ", s)        # 61个文件（兜底）
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
# 汉字片段（用于取出一串中文转拼音）
_CN_CHUNK = re.compile(r"[一-鿿]+")


def _cjk_core(s: str) -> str:
    return _CJK_CORE.sub("", s)


def _latin_core(s: str) -> str:
    return _LATIN_CORE.sub("", s)


def _cn_pinyin(s: str) -> str:
    """取字符串里的中文字符，转成无声调小写拼音（黑夜告白 → heiyegaobai）。"""
    if not _HAS_PINYIN:
        return ""
    segs = _CN_CHUNK.findall(s or "")
    if not segs:
        return ""
    try:
        return "".join(lazy_pinyin("".join(segs)))
    except Exception:  # noqa: BLE001 - 转换失败当作无拼音
        return ""


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
    """资源标题的质量分（越大越好）。用于洗版时判断「要不要替换盘里已有的旧版本」。"""
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

    只作「无单集签名资源（电影 / 多集包）」的模糊兜底匹配，三套核心核依次匹配：
    - 同语言同片（中文核/英文核精确相等，含续集数字）
    - 中英混合标题（一侧只有中文、另一侧只有英文时仍能命中）
    - 续集区分（流浪地球 vs 流浪地球2 不会误判为同一部）
    - 追剧关键：带集号的新资源 vs 不带集号的云端目录（可能是整包/整季）→ 一律不判为
      同一集（宁可在首次重复时多转一份，也绝不漏集）；双方集号不同 → 不是同一集。
    """
    a = title_core(resource_title)
    b = title_core(existing_name)
    if not a or not b:
        return False
    ea, eb = episode_sig(resource_title), episode_sig(existing_name)
    if ea and eb and ea != eb:
        return False
    # 新资源带集号、对端目录不带集号：无法证明对端包含这一集（可能是整包），不判同片。
    # 否则「云端已落整包《庆余年.2023》」会把新出的第6集误杀掉。
    if ea and not eb:
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
    # 拼音兜底：一侧是中文，另一侧是纯拉丁拼音名（黑夜告白 ↔ HeiYeGaoBai）。
    if _HAS_PINYIN and bool(_CJK.search(a)) != bool(_CJK.search(b)):
        for cn_side, lat_side in ((a, b), (b, a)):
            pa = _cn_pinyin(cn_side)
            pl = _latin_core(lat_side)
            if len(pa) >= 4 and pl and pl == pa:
                return True
    return False


from dataclasses import dataclass  # noqa: E402


@dataclass
class DedupDecision:
    action: str          # transfer / skip_exists / retransfer / upgrade
    reason: str
    category: str
    parent_id: str = ""
    replace_file_id: str = ""    # 洗版：待删除的旧版本 fileId
    replace_parent_id: str = ""  # 洗版：旧版本所在目录 fileId


_VIDEO_EXT = re.compile(r"\.(mkv|mp4|avi|ts|rmvb|rm|iso|mov|wmv|flv|m2ts)$", re.I)


def _entry_base(name: str) -> str:
    """去掉条目名的文件扩展名（文件夹名通常没有）。"""
    return _VIDEO_EXT.sub("", name or "")


def _entry_norm(name: str) -> str:
    return "".join(_ALNUM.findall(_entry_base(name).lower()))


class CloudDedup:
    """三级去重决策器（hash → 账本 → 云端）。"""

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
        self._dir_cache: dict = {}

    # ---------- 云端目录列表（带缓存，存完整条目）----------
    def _list_dir_entries(self, parent_id: str) -> list:
        now = time.time()
        with self._lock:
            hit = self._dir_cache.get(parent_id)
            if hit and now - hit[0] < self.cache_ttl:
                return hit[1]
        try:
            entries = self.client.list_dir(parent_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("云端列举目录失败（%s），本次跳过云端查重", exc)
            return []
        for e in entries:
            e.setdefault("parent_id", parent_id)
        with self._lock:
            self._dir_cache[parent_id] = (now, entries)
        return entries

    def _find_existing(self, category: str, info) -> dict | None:
        """在云端找同名同内容条目。先精确（我们的标准 folder 名），再模糊（历史命名兜底）。

        返回条目 dict（含 file_id / name / parent_id），找不到返回 None。
        候选目录 = 分类目录 + 转存根目录（旧版本/未分类/其它下载器可能平铺在根目录）。
        文件夹与散文件都会进入比对（磁力离线落盘大多是一个文件夹）。
        """
        parents: list[str] = []
        if category and self.resolver.exists(category):
            pid, _ = self.resolver.resolve(category, create_missing=False)
            if pid:
                parents.append(pid)
        if self.resolver.root_id and self.resolver.root_id not in parents:
            parents.append(self.resolver.root_id)  # 根目录兜底，防平铺资源漏判

        # ① 精确：云端名字 == 我们这次的标准 folder（同内容不同源也应同名）
        for parent_id in parents:
            for e in self._list_dir_entries(parent_id):
                name = e.get("name") or ""
                if name and _entry_norm(name) == info.key:
                    return e

        # ② 模糊兜底（历史遗留命名）。单集 vs 无集号目录的误杀风险
        #    已由 names_match 内部规则挡住：新资源带集号、对端目录不带集号 → 一律不判同集。
        for parent_id in parents:
            for e in self._list_dir_entries(parent_id):
                name = e.get("name") or ""
                if not name:
                    continue
                if names_match(info.title, name):
                    return e
        return None

    def decide(self, hash_: str, title: str, store) -> DedupDecision:
        """综合本地记录 + 内容账本 + 云端复查，给出去重决策。

        store 需提供 get / title_exists / title_has_episodes。
        """
        cat = ""
        try:
            cat = self.classifier.classify(title).category if self.organize_enabled else ""
        except Exception:  # noqa: BLE001
            pass
        info = analyze(title)

        rec = None
        try:
            rec = store.get(hash_)
        except Exception:  # noqa: BLE001
            rec = None
        local_done = rec is not None and (rec.status in ("done", "submitted", "upgraded"))

        # ① 同一磁力已处理过 → 直接跳过（云端仅用于洗版判定）
        if local_done:
            if self.cloud_check_new and self.upgrade:
                try:
                    existing = self._find_existing(cat, info)
                except Exception:  # noqa: BLE001
                    existing = None
                if existing:
                    return self._decide_upgrade(existing, cat, title,
                                                "本地已转存且云端仍存在")
            return DedupDecision(
                "skip_exists",
                "本地已转存过该磁力，跳过（防重复）", cat, "")

        # ② 内容账本命中：同内容曾成功落盘（可能是不同磁力/不同写法）
        ledger = None
        try:
            if bool(info.key) and hasattr(store, "title_get"):
                ledger = store.title_get(info.key)
        except Exception:  # noqa: BLE001
            ledger = None
        if ledger is not None:
            if self.upgrade:
                new_q = quality_score(title)
                old_q = int(getattr(ledger, "quality", 0) or 0)
                # 新版本质量更高才洗版：替换云端旧 folder（账本记得旧版本的真实质量，
                # 新规范命名的文件夹名不含分辨率，不能拿文件夹名估旧质量）
                if new_q > old_q:
                    try:
                        existing = self._find_existing(cat, info)
                    except Exception:  # noqa: BLE001
                        existing = None
                    if existing:
                        return DedupDecision(
                            "upgrade",
                            f"账本有旧版本（质量 {old_q}），新版本更优（{new_q}），洗版替换",
                            cat, "",
                            replace_file_id=existing.get("file_id", ""),
                            replace_parent_id=existing.get("parent_id", ""),
                        )
            return DedupDecision(
                "skip_exists",
                "账本已记录该内容（曾成功落盘），跳过", cat, "")

        # ②b 整包 vs 已按集收录：先逐集追过的剧，再来「全集包」多为重复 → 跳过
        if info.is_pack and not info.sig:
            try:
                has_ep = store.title_has_episodes(norm_name(info.core)) \
                    if hasattr(store, "title_has_episodes") else False
            except Exception:  # noqa: BLE001
                has_ep = False
            if has_ep:
                return DedupDecision(
                    "skip_exists",
                    "该剧已按集收录过，整集包多为重复，跳过（如含新增集可手动转存）", cat, "")

        # ③ 云端复查（首见内容，防本程序之外的历史资源）
        if self.cloud_check_new:
            try:
                existing = self._find_existing(cat, info)
            except Exception:  # noqa: BLE001
                existing = None
            if existing:
                if self.upgrade:
                    return self._decide_upgrade(existing, cat, title,
                                                "云端已存在同名资源")
                return DedupDecision("skip_exists", "云端已有同名资源，跳过", cat, "")
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
