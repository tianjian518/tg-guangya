"""去重决策：接到链接 → 规范命名 → 对比云盘 → 已存放弃 / 没存落盘。

判定真源是【云盘】，不是本地记录：
  1. 同一磁力 hash 已在本机处理过（magnets 表）→ 防刷屏兜底：
       - 云端能找到对应内容 → 跳过；
       - 云端找不到（本地显示 done、但盘里已没有 → 可能被手动删/清理）→ 重新落盘 retransfer；
       - 任务还在进行中（submitted/upgraded）→ 跳过，等后台任务跑完，避免重复提交。
  2. 已按集收录过的剧再来「全集包」→ 跳过（多为重复整包）。
  3. 云端复查（真正的“已存就放弃”依据，用 core/ident.py 的规范 folder 名精确比对，
     老命名用 names_match 模糊兜底）：
        - 命中 → 已存：跳过；开了洗版且新版本质量更优 → 删除旧版替换；
        - 未命中 → 云盘没有 → 落盘 transfer。
   4. 落盘准入（中文规范 + 归类，做不到就放弃 reject）：
        - 标题必须剥得出中文片名（core 含中文），否则无法中文规范命名 → 放弃；
        - 必须能整理进明确的中文分类目录（不进「未分类/其他」这类兜底桶），
          且自动整理必须开启，否则 → 放弃。
     开关：dedup.require_cn（默认开）。

规范命名的保证：
- 落盘 folder 名 = ident 的确定性 folder（电影/整包=片名.年份，单集/季=片名.SxxExx/.Sxx）。
- 集数签名进 folder → 第6集永远不会被第5集顶掉；同一集被不同磁力重复发布 →
  云盘里那个 S01E06 folder 在 → 直接跳过。
- 本地 titles 账本不再当「永久跳过黑名单」，只做两件事：
  洗版时提供旧版本的真实质量分（新规范 folder 名不含分辨率，不能从名字估）；
  以及记录历史（防「云端整包目录不带集号 → 误杀后续新集」时仍能区分）。

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
from core.classifier import KIND_OTHER  # noqa: E402

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

# 标题开头的类型标签：如【电影】 / [剧集] / （电影）/ 电影 / 剧集 流浪地球 / 欧美电影: xxx
# 与 core/ident.py 的 _LEADING_TAG 保持同一份词表，避免两处口径不一致。
_LEADING_TAG = re.compile(
    r"^\s*[\[【(（]?\s*"
    r"(?:最新|热门|经典|精品|推荐|高清|超清|蓝光|原盘|4k|8k|uhd|1080p|720p|2160p)?\s*"
    r"(?:欧美电影|华语电影|国产电影|外语电影|亚洲电影|日本电影|韩国电影|港台电影|"
    r"电影|剧集|动漫|动画|纪录片|综艺|演唱会|短片|连续剧|网剧|电视剧|"
    r"美剧|韩剧|日剧|港剧|台剧|国产剧|欧美剧|日韩剧|港片|日影|韩影)"
    r"\s*[\]】)）］]?\s*[:：|｜\-–—]?\s*",
    re.I,
)


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
    action: str          # transfer / skip_exists / retransfer / upgrade / reject
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


# 落盘准入检查：必须「中文规范命名 + 整理归类」都做到，否则放弃链接。
# 用户媒体库是中文体系：盘里要的是能认的中文片名、放进明确的中文分类目录；
# 标题剥不出中文片名（纯英文/纯拼音/乱码），或只能归进「未分类/其他」这类
# 兜底桶（=没整理成功），一律不落盘 —— 宁缺毋滥，做不到就放弃这个链接。
def _standard_block_reason(info, cr, cat: str, organize_enabled: bool,
                           require_cn: bool, unknown_dir: str) -> str:
    """返回放弃理由（空串 = 可以通过，允许落盘）。"""
    if not require_cn:
        return ""
    if not organize_enabled or cr is None:
        return "自动整理归类未开启（组织分类被关闭）"
    # ① 中文规范命名：片名主体必须含中文（core 是全中文时 ident 已剥掉英文，
    #    如 奥本海默；纯英文/纯拼音标题做不成中文片名）
    if not re.search(_CJK, info.core or ""):
        return "标题无法规范成中文片名（缺中文片名），放弃链接"
    # ② 整理归类：分类必须是明确的中文内容目录
    if not cr.category:
        return "无法整理归类（分类结果为空），放弃链接"
    if cr.kind == KIND_OTHER or (cr.category or "") == (unknown_dir or "未分类"):
        return f"只能归入兜底类「{cr.category}」，不算整理成功，放弃链接"
    return ""


class CloudDedup:
    """三级去重决策器（hash → 账本 → 云端）。"""

    def __init__(self, client, resolver, classifier,
                 cloud_check_new: bool = True, cache_ttl: float = 300.0,
                 organize_enabled: bool = True, upgrade: bool = False,
                 require_cn: bool = True) -> None:
        self.client = client
        self.resolver = resolver
        self.classifier = classifier
        self.cloud_check_new = cloud_check_new
        self.cache_ttl = cache_ttl
        self.organize_enabled = organize_enabled
        self.upgrade = upgrade
        self.require_cn = require_cn
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

        # ① 精确：云端名字 == 我们这次的标准 folder（同内容不同源也应同名）。
        #    两侧都归一化：info.key 形如「庆余年.S01E06 / 流浪地球2.2023」，
        #    云端 folder 名与其规范名一致 → 归一化后相等，才算“已存”。
        norm_key = _entry_norm(info.key or "")
        if norm_key:
            for parent_id in parents:
                for e in self._list_dir_entries(parent_id):
                    name = e.get("name") or ""
                    if name and _entry_norm(name) == norm_key:
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
        """综合本地记录 + 云端复查，给出去重决策。

        判定真源 = 云盘：云盘里已有同内容 → 放弃；没有 → 再查「中文规范准入」：
        - 标题剥得出中文片名、且能整理进明确的中文分类目录 → 落盘 transfer；
        - 做不到（无中文片名 / 只能进兜底类 / 整理被关闭）→ reject，放弃链接。
        store 需提供 get / title_has_episodes / title_get。
        """
        info = analyze(title)
        cr = None
        cat = ""
        if self.organize_enabled:
            try:
                # 原始标题 + 规范中文名一起喂给分类器：
                # 地区信号（英语/日语/韩语/栏目前缀/英文原名）只在原始标题里，
                # 中文名只作形态与地名的补充。只传中文 folder 会让所有外语片判成华语。
                cr = self.classifier.classify(title, extra=info.folder)
                cat = cr.category
            except Exception:  # noqa: BLE001
                cr = None

        rec = None
        try:
            rec = store.get(hash_)
        except Exception:  # noqa: BLE001
            rec = None
        local_done = rec is not None and (rec.status in ("done", "submitted", "upgraded"))

        def _standard_reason() -> str:
            return _standard_block_reason(
                info, cr, cat, self.organize_enabled, self.require_cn,
                getattr(self.classifier, "unknown_dir", "未分类"))

        # ① 同一磁力已处理过 → 防刷屏兜底（真正的“是否已存”仍以云盘为准）
        if local_done:
            if self.cloud_check_new:
                try:
                    existing = self._find_existing(cat, info)
                except Exception:  # noqa: BLE001
                    existing = None
                if existing:
                    return self._decide_upgrade(existing, cat, title, store, info,
                                                "本地已转存过该磁力，云盘里也有")
                # 云盘里没有：本地显示 done 说明曾落盘成功 → 盘里那份被删/移走了，
                # 按“云盘为准”重新落盘（重新落盘同样是新写一份，也要过中文规范准入）；
                # 若任务还在跑则继续等，不重复提交。
                if rec.status == "done":
                    blk = _standard_reason()
                    if blk:
                        return DedupDecision("reject", blk, cat or "", "")
                    return DedupDecision(
                        "retransfer",
                        "本地曾完成转存但云盘已无该内容，重新落盘", cat, "")
                return DedupDecision(
                    "skip_exists",
                    "该磁力任务仍在进行中，跳过（防重复提交）", cat, "")
            return DedupDecision(
                "skip_exists",
                "本地已处理过该磁力（关闭云端复查），跳过", cat, "")

        # ② 已按集收录过的剧，再来「全集包」→ 多为重复整包 → 跳过
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

        # ③ 云端复查 = “对比云盘里是否已存”（核心判定）
        #    先精确 folder 名（我们自己规范命名的标准名），再 names_match 模糊兜底
        #    （MoviePilot / 老版本等历史遗留命名）。
        if self.cloud_check_new:
            try:
                existing = self._find_existing(cat, info)
            except Exception:  # noqa: BLE001
                existing = None
            if existing:
                return self._decide_upgrade(existing, cat, title, store, info,
                                            "云端已存在同名资源")

        # ④ 落盘前准入：中文规范命名 + 整理归类，做不到就放弃这个链接
        blk = _standard_reason()
        if blk:
            return DedupDecision("reject", blk, cat or "", "")
        return DedupDecision("transfer", "云盘没有该内容，落盘", cat, "")

    def _decide_upgrade(self, existing: dict, cat: str, title: str,
                        store, info, base_reason: str) -> DedupDecision:
        """云端已存在同内容时的决策：已存 → 放弃（默认）；开洗版且新版本更优 → 替换。

        旧版本质量优先取账本记录（落盘时标题的质量分）——新规范命名的 folder 名不含
        分辨率，不能拿 folder 名估；没有账本记录（不是本程序转的）才从资源名估。
        """
        if not self.upgrade:
            return DedupDecision("skip_exists", f"{base_reason}，跳过", cat, "")
        ledger = None
        try:
            if bool(info.key) and hasattr(store, "title_get"):
                ledger = store.title_get(info.key)
        except Exception:  # noqa: BLE001
            ledger = None
        if ledger is not None:
            old_q = int(getattr(ledger, "quality", 0) or 0)
        else:
            old_q = quality_score(existing.get("name", ""))
        new_q = quality_score(title)
        if new_q <= old_q:
            return DedupDecision("skip_exists", f"{base_reason}，新版本质量不更优，跳过",
                                 cat, "")
        # 旧版本质量信息完全不可知（非本程序转的、folder 名又不带分辨率）→ 保守不删
        if old_q == 0 and ledger is None:
            return DedupDecision("skip_exists",
                                 f"{base_reason}，旧版本质量未知，不轻易替换", cat, "")
        return DedupDecision(
            "upgrade",
            f"{base_reason}；新版本质量更优（{new_q} > {old_q}），洗版替换旧版本",
            cat, "",
            replace_file_id=existing.get("file_id", ""),
            replace_parent_id=existing.get("parent_id", ""),
        )
