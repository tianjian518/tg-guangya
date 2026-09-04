"""影视资源自动分类：把频道里的资源判进「内容形态 + 地区」子目录。

分类依据（参考主流媒体库实践）：
  - NASTool / Emby / Jellyfin 中文社区：一级按内容形态（电影 / 电视剧 / 动漫），
    二级按地区（华语电影、外语电影、动画电影）；
  - 网盘分享与字幕组的实际用法：扁平的「地区 + 类型」，如 华语电影 / 欧美电影 / 国产剧 / 日韩剧。
本模块两种都支持：structure=flat（扁平，默认）或 two_level（电影/华语 两级）。

判定采用**打分制**而非硬规则：频道标题往往不规范（缺语言标签、中英混排、带水印），
打分可以在信息不足时给出次优解，并附上置信度与命中信号，便于排查。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ---------- 维度定义 ----------

KIND_MOVIE = "movie"
KIND_TV = "tv"
KIND_ANIME = "anime"
KIND_DOC = "documentary"
KIND_VARIETY = "variety"
KIND_MUSIC = "music"
KIND_OTHER = "other"

REGION_CN = "cn"
REGION_HKTW = "hktw"
REGION_JPKR = "jpkr"
REGION_WEST = "west"
REGION_OTHER = "other"

KIND_NAMES = {
    KIND_MOVIE: "电影", KIND_TV: "剧集", KIND_ANIME: "动漫",
    KIND_DOC: "纪录片", KIND_VARIETY: "综艺", KIND_MUSIC: "演唱会", KIND_OTHER: "其他",
}
REGION_NAMES = {
    REGION_CN: "华语", REGION_HKTW: "港台", REGION_JPKR: "日韩",
    REGION_WEST: "欧美", REGION_OTHER: "其他",
}

# 扁平结构：目录名直接体现「地区 + 类型」
DEFAULT_MAPPING: dict[tuple[str, str], str] = {
    (KIND_MOVIE, REGION_CN): "华语电影",
    (KIND_MOVIE, REGION_WEST): "欧美电影",
    (KIND_MOVIE, REGION_JPKR): "日韩电影",
    (KIND_MOVIE, REGION_OTHER): "其他电影",
    (KIND_TV, REGION_CN): "国产剧",
    (KIND_TV, REGION_WEST): "欧美剧",
    (KIND_TV, REGION_JPKR): "日韩剧",
    (KIND_TV, REGION_OTHER): "其他剧集",
    (KIND_ANIME, REGION_CN): "国产动漫",
    (KIND_ANIME, REGION_WEST): "欧美动漫",
    (KIND_ANIME, REGION_JPKR): "日本动漫",
    (KIND_ANIME, REGION_OTHER): "其他动漫",
    (KIND_DOC, REGION_CN): "纪录片",
    (KIND_DOC, REGION_WEST): "纪录片",
    (KIND_DOC, REGION_JPKR): "纪录片",
    (KIND_DOC, REGION_OTHER): "纪录片",
    (KIND_VARIETY, REGION_CN): "综艺",
    (KIND_VARIETY, REGION_WEST): "综艺",
    (KIND_VARIETY, REGION_JPKR): "综艺",
    (KIND_VARIETY, REGION_OTHER): "综艺",
    (KIND_MUSIC, REGION_CN): "演唱会",
    (KIND_MUSIC, REGION_WEST): "演唱会",
    (KIND_MUSIC, REGION_JPKR): "演唱会",
    (KIND_MUSIC, REGION_OTHER): "演唱会",
    (KIND_OTHER, REGION_CN): "其他",
    (KIND_OTHER, REGION_WEST): "其他",
    (KIND_OTHER, REGION_JPKR): "其他",
    (KIND_OTHER, REGION_OTHER): "其他",
}

# 两级结构：一级「内容形态」/ 二级「地区」
LEVEL1_NAMES = {
    KIND_MOVIE: "电影", KIND_TV: "电视剧", KIND_ANIME: "动漫",
    KIND_DOC: "纪录片", KIND_VARIETY: "综艺", KIND_MUSIC: "演唱会", KIND_OTHER: "其他",
}
LEVEL2_NAMES = REGION_NAMES

DEFAULT_UNKNOWN_DIR = "未分类"


# ---------- 规则表 ----------
# 分两类是有意为之：分辨率/格式（1080P、BluRay、年份）几乎出现在**所有**资源里，
# 属于通用信号。若让它们和「演唱会」「综艺」这类专有词同台竞争，专有类型会被压过去
# （实测出现过「周杰伦 演唱会」被判成电影——电影规则累加到了 5 分）。所以：
#   1. 先只用【专有类型规则】判定（纪录片/演唱会/综艺/动漫/剧集）；
#   2. 专有规则都没命中时，才用【电影规则 + 兜底启发式】。

_SPECIFIC_KIND_RULES: list[tuple[re.Pattern, str, int]] = [
    # 纪录片
    (re.compile(r"纪录片|纪录电影|documentary|国家地理|national\s*geographic|探索频道|discovery\s*channel|nhk|bbc\s*(纪录片|纪录)|人文历史|自然纪录", re.I), KIND_DOC, 4),
    # 演唱会 / 音乐
    (re.compile(r"演唱会|音乐会|音乐节|concert|live\s*show|mv\s*合集|专辑|钢琴|交响|歌友会|巡演|tour\s*live", re.I), KIND_MUSIC, 4),
    # 综艺（「第X期」是综艺的标志性说法，剧集用「第X集」）
    (re.compile(r"综艺|真人秀|脱口秀|选秀|晚会|春晚|跨年|talk\s*show|variety\s*show|音乐综艺|喜剧大会|喜剧大赛|吐槽大会", re.I), KIND_VARIETY, 4),
    (re.compile(r"第\s*\d{1,3}\s*期|更新至\s*\d{1,3}\s*期|歌手|好声音|奔跑吧|极限挑战|王牌对王牌|向往的生活|乘风破浪|创造营|青春环游记|密室大逃脱|大侦探|名侦探学院|花儿与少年|妻子的浪漫旅行|令人心动的|声生不息|我们的歌|时光音乐会|披荆斩棘|这就是街舞|中国新说唱|乐队的夏天|奇葩说|圆桌派|十三邀|鲁豫有约|康熙来了|快乐再出发|种地吧|running\s*man|无限挑战|新西游记|两天一夜|认识的哥哥|请回答", re.I), KIND_VARIETY, 3),
    # 动漫（含一批高频番名，权重高于「第X集」这类通用剧集信号，避免日番被判成剧集）
    (re.compile(r"动漫|动画|anime|剧场版|\bova\b|\boad\b|番剧|新番|国产动画|日本动画|海贼王|火影忍者|名侦探柯南|哆啦a梦|蜡笔小新|樱桃小丸子|精灵宝可梦|宝可梦|鬼灭之刃|咒术回战|进击的巨人|间谍过家家|葬送的芙莉莲|字幕组|喵萌|诸神|甜梦|动漫花园|巴哈姆特", re.I), KIND_ANIME, 4),
    # 剧集（季集标记）
    (re.compile(r"\bs\d{1,2}\s*[-_.]?\s*e?\d{0,3}\b|\bseason\s*\d+|\bep?\d{1,3}\b|第\s*\d{1,3}\s*[集话話]|全集|更新至|更新到|连载中|\d{1,2}\s*[-~]\s*\d{1,3}\s*集|共\s*\d+\s*集|剧集|电视剧|网剧|连续剧|tv\s*series", re.I), KIND_TV, 3),
    (re.compile(r"韩剧|日剧|美剧|英剧|泰剧|港剧|台剧|国产剧|漫改剧", re.I), KIND_TV, 2),
]

# 电影规则：全靠「年份 + 发行格式」这类通用信号，仅在专有规则未命中时兜底
_MOVIE_RULES: list[tuple[re.Pattern, str, int]] = [
    (re.compile(r"(19|20)\d{2}.*(1080p|2160p|4k|720p|bluray|blu-?ray|bdrip|remux|web-?dl|webrip|hdr|dv|imax)", re.I), KIND_MOVIE, 3),
    (re.compile(r"电影|影片|院线|影院版|导演剪辑版|蓝光|bd\s*rip|1080p|2160p|4k\s*(hdr|uhd)?|hd\s*电影", re.I), KIND_MOVIE, 2),
]

# 日本动漫特征词（番名 + 圈内术语）。动漫标题常不带语言标签，
# 只靠中文占比会把日番判成国产动漫，故单独给一条日韩线索（权重 3）。
_ANIME_JPKR = re.compile(
    r"\bova\b|\boad\b|剧场版|新番|番剧|字幕组|喵萌|诸神|甜梦|动漫花园|巴哈姆特|"
    r"海贼王|火影忍者|名侦探柯南|哆啦a梦|蜡笔小新|樱桃小丸子|精灵宝可梦|宝可梦|"
    r"鬼灭之刃|咒术回战|进击的巨人|间谍过家家|葬送的芙莉莲|初音未来|索尼子",
    re.I,
)

_REGION_RULES: list[tuple[re.Pattern, str, int]] = [
    # 港台：直接并入华语（不单独设类，港台影视统一归入华语目录）
    (re.compile(r"粤语|广东话|繁体|繁中|香港|台湾|hktv|tvb|港剧|台剧|cantonese|hong\s*kong|taiwan", re.I), REGION_CN, 4),
    # 日韩
    (re.compile(r"日语|韩语|韩剧|日剧|日本|韩国|日影|韩影|jpn|kor|日语中字|韩语中字|日版|韩版|首尔|东京|korean|japanese", re.I), REGION_JPKR, 4),
    # 欧美
    (re.compile(r"英语|英文|美剧|英剧|欧美|英语中字|英文原声|usa|us\s*版|uk|hollywood|english|西班牙语|法语|德语|俄语|意大利语|好莱坞", re.I), REGION_WEST, 4),
    (re.compile(r"美国|英国|法国|德国|西班牙|意大利|俄罗斯|欧洲|加拿大|澳大利亚", re.I), REGION_WEST, 2),
    # 华语
    (re.compile(r"国语|普通话|中文|国配|中字|简体|简中|chs|cht|大陆|内地|国产|华语|中国|央视", re.I), REGION_CN, 4),
    # 其他地区
    (re.compile(r"泰语|泰剧|印度|越南|泰国|新加坡|马来西亚|印尼|菲律宾|土耳其|伊朗|阿拉伯", re.I), REGION_OTHER, 3),
    # 日本动漫特征词（番名/术语）。动漫标题常缺语言标签，靠它把日番从国产动漫里分出来。
    (_ANIME_JPKR, REGION_JPKR, 3),
]

_CJK = re.compile(r"[\u4e00-\u9fff]")
_NOISE = re.compile(r"\s+")

# 频道栏目前缀：【欧美电影】/【日韩剧】/ 欧美电影: xxx …
# 词表与 core/ident.py 的 _LEADING_TAG 保持一致（两处必须同步改）。
_LEADING_CATEGORY = re.compile(
    r"^\s*[\[【(（]?\s*"
    r"(?:最新|热门|经典|精品|推荐|高清|超清|蓝光|原盘|4k|8k|uhd|1080p|720p|2160p)?\s*"
    r"(欧美电影|华语电影|国产电影|外语电影|亚洲电影|日本电影|韩国电影|港台电影|"
    r"电影|剧集|动漫|动画|纪录片|综艺|演唱会|短片|连续剧|网剧|电视剧|"
    r"美剧|韩剧|日剧|港剧|台剧|国产剧|欧美剧|日韩剧|港片|日影|韩影)"
    r"\s*[\]】)）］]?\s*[:：|｜\-–—]?\s*",
    re.I,
)

# 栏目前缀里的地区词 → 地区。这是「频道编辑的显式标注」，比语言推断可靠，
# 但弱于真正的语言标签（粤语/日语/韩语…），所以权重只给 2。
_PREFIX_REGION: list[tuple[re.Pattern, str]] = [
    (re.compile(r"日韩|日本|韩国", re.I), REGION_JPKR),
    (re.compile(r"欧美|外语", re.I), REGION_WEST),
    (re.compile(r"国产|华语|港台", re.I), REGION_CN),
]


def _cjk_ratio(text: str) -> float:
    """中文字符占非空字符的比例，用于无语言标签时的兜底推断。"""
    s = _NOISE.sub("", text or "")
    if not s:
        return 0.0
    return len(_CJK.findall(s)) / len(s)


@dataclass
class ClassifyResult:
    kind: str = KIND_OTHER
    region: str = REGION_OTHER
    category: str = ""          # 最终目录名（两级时为 "电影/华语"）
    confidence: float = 0.0     # 0~1
    signals: list[str] = field(default_factory=list)

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, self.kind)

    @property
    def region_name(self) -> str:
        return REGION_NAMES.get(self.region, self.region)


class Classifier:
    """把标题判进某个分类子目录。

    :param mapping:     自定义映射 {(kind, region): 目录名}，留空用内置默认
    :param structure:   flat（扁平）或 two_level（一级/二级）
    :param unknown_dir: 无法判定时的归属目录
    """

    def __init__(
        self,
        mapping: dict | None = None,
        structure: str = "flat",
        unknown_dir: str = DEFAULT_UNKNOWN_DIR,
        extra_kind_rules: Iterable | None = None,
        extra_region_rules: Iterable | None = None,
    ) -> None:
        self.structure = "two_level" if str(structure).strip().lower() == "two_level" else "flat"
        self.unknown_dir = (unknown_dir or "").strip() or DEFAULT_UNKNOWN_DIR
        self.mapping: dict[tuple[str, str], str] = {}
        # 配置里映射键写作 "movie:cn"，内置默认是 (kind, region) 元组，两种都兼容
        for key, name in (mapping or DEFAULT_MAPPING).items():
            if isinstance(key, str):
                k, _, r = key.partition(":")
            else:
                k, r = (list(key) + ["", ""])[:2]
            self.mapping[(str(k).strip(), str(r).strip())] = str(name).strip()
        # 补齐内置默认，避免自定义映射只写了部分导致落到未分类
        for key, name in DEFAULT_MAPPING.items():
            self.mapping.setdefault(key, name)
        self.kind_rules = list(_SPECIFIC_KIND_RULES) + list(extra_kind_rules or [])
        self.movie_rules = list(_MOVIE_RULES)
        self.region_rules = list(_REGION_RULES) + list(extra_region_rules or [])

    # ---------- 主入口 ----------
    def classify(self, title: str, extra: str = "") -> ClassifyResult:
        """判定分类。

        :param title: 原始频道标题（保留栏目前缀、语言标签、英文原名——
                      这些是地区判定的真实信号，绝不能先剥掉）
        :param extra: 补充文本，通常是 ident 产出的规范中文名 info.folder

        地区判定优先级（高 → 低）：
          1. 显式语言/地区标签：粤语/日语/韩语/英语/国语/美国/东京…（权重 4）
          2. 频道栏目前缀里的地区词：日韩剧/欧美电影/国产剧…（权重 2）
          3. 原标题语言推断：几乎无中文→欧美；中文为主→华语（权重 2）

        ⚠️ 历史 bug：曾把「已翻译成中文的 folder」单独喂进来，靠它的中文占比
        推断地区，结果所有外语片的中文译名都被判成华语（黑客帝国→华语电影、
        盗梦空间→华语电影）。中文占比必须只看「原始标题」，不能看中文译名。
        """
        text = f"{title or ''} {extra or ''}"[:600]
        if not text.strip():
            return self._unknown("空标题")

        # 形态：先判专有类型（纪录片/演唱会/综艺/动漫/剧集），都没命中再按电影兜底
        kind, kind_score, kind_sig = self._score(self.kind_rules, text)
        if not kind:
            kind, kind_score, kind_sig = self._score(self.movie_rules, text)
        region, region_score, region_sig = self._score(self.region_rules, text)

        signals = list(kind_sig) + list(region_sig)

        # 形态兜底：没抓到明确信号时，用季集标记 / 年份推断
        if not kind or kind_score == 0:
            if re.search(r"\bs\d{1,2}\s*[-_.]?\s*e?\d{0,3}\b|第\s*\d{1,3}\s*[集话話]|全集|更新至|连载中", text, re.I):
                kind, kind_score = KIND_TV, 1
                signals.append("兜底:出现集数标记→剧集")
            elif re.search(r"(19|20)\d{2}", text):
                kind, kind_score = KIND_MOVIE, 1
                signals.append("兜底:出现年份→电影")
            else:
                kind, kind_score = KIND_MOVIE, 1
                signals.append("兜底:无明确信号→按电影处理")

        # 地区兜底：显式语言标签没命中时，依次退到「栏目前缀」→「原标题语言」
        if not region or region_score == 0:
            prefix = self._prefix_region(title)
            if prefix:
                region, region_score = prefix, 2
                signals.append(f"栏目前缀→{REGION_NAMES.get(prefix, prefix)}")
            else:
                # 中文占比只看原始标题（剥掉栏目前缀）。绝不能用 text——
                # 它含已翻译成中文的 folder，会让所有外语片译名都判成华语。
                base = _LEADING_CATEGORY.sub("", title or "").strip() or (title or "")
                ratio = _cjk_ratio(base)
                if ratio >= 0.25:
                    region, region_score = REGION_CN, 2
                    signals.append(f"兜底:原标题以中文为主({ratio:.0%})→华语")
                elif ratio <= 0.05:
                    region, region_score = REGION_WEST, 2
                    signals.append(f"兜底:原标题几乎无中文({ratio:.0%})→欧美")
                else:
                    region, region_score = REGION_OTHER, 1
                    signals.append(f"兜底:原标题中英混排({ratio:.0%})→其他")

        category = self._to_category(kind, region)
        # 置信度：两套打分归一化后取平均，上限 1
        confidence = round(min(1.0, (min(kind_score, 4) / 4 + min(region_score, 4) / 4) / 2), 2)
        return ClassifyResult(kind=kind, region=region, category=category,
                              confidence=confidence, signals=signals)

    # ---------- 内部 ----------
    @staticmethod
    def _prefix_region(title: str) -> str:
        """从频道栏目前缀（【日韩剧】/【欧美电影】/ 欧美电影: …）里取地区。

        频道编辑标注的栏目名是可信线索，但弱于真正的语言标签，取不到就返回空串。
        """
        m = _LEADING_CATEGORY.match(title or "")
        if not m:
            return ""
        tag = m.group(1) or ""
        for pat, region in _PREFIX_REGION:
            if pat.search(tag):
                return region
        return ""

    def _score(self, rules, text: str) -> tuple[str, int, list[str]]:
        scores: dict[str, int] = {}
        signals: list[str] = []
        for pat, target, weight in rules:
            m = pat.search(text)
            if m:
                scores[target] = scores.get(target, 0) + weight
                hit = (m.group(0) or "").strip()
                if hit and len(hit) < 30:
                    signals.append(f"{hit}→{target}")
        if not scores:
            return "", 0, []
        best = max(scores.items(), key=lambda kv: kv[1])
        return best[0], best[1], signals

    def _to_category(self, kind: str, region: str) -> str:
        name = self.mapping.get((kind, region))
        if not name:
            name = self.mapping.get((kind, REGION_OTHER)) or self.unknown_dir
        if self.structure == "two_level":
            l1 = LEVEL1_NAMES.get(kind, LEVEL1_NAMES[KIND_OTHER])
            # 纪录片/综艺/演唱会这类不区分地区的，只建一级，避免出现「演唱会/华语」这种冗余嵌套
            region_names = {v for (k, _r), v in self.mapping.items() if k == kind}
            if len(region_names) <= 1:
                return l1
            l2 = LEVEL2_NAMES.get(region, LEVEL2_NAMES[REGION_OTHER])
            return f"{l1}/{l2}"
        return name or self.unknown_dir

    def _unknown(self, reason: str) -> ClassifyResult:
        return ClassifyResult(kind=KIND_OTHER, region=REGION_OTHER,
                              category=self.unknown_dir, confidence=0.0, signals=[reason])


def default_mapping_flat() -> list[dict]:
    """给面板用的默认映射表（扁平结构）。"""
    seen: list[dict] = []
    for (kind, region), name in DEFAULT_MAPPING.items():
        if any(x["name"] == name for x in seen):
            continue
        seen.append({
            "name": name,
            "kind": kind,
            "kind_name": KIND_NAMES.get(kind, kind),
            "region": region,
            "region_name": REGION_NAMES.get(region, region),
        })
    return seen
