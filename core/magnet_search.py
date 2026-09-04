"""磁力搜索引擎（供 TG 机器人 /s 搜索用）。

机器人能直接搜「全网磁力种子」而不是等频道更新：`/s 片名` 返回一批种子，
点按钮或发链接即可一键转存光鸭。搜索发生在程序运行的这台机器上。

默认引擎 apibay.org（The Pirate Bay 的公开 API）：
    GET https://apibay.org/q.php?q=<关键词>&cat=0  →  JSON 数组
    [{name, info_hash, seeders, leechers, size, category, imdb, ...}]
    磁力 = magnet:?xt=urn:btih:<info_hash>&dn=<name>

注意（部署这台机器的同学）：
- 搜索引擎域名一般被污染/封锁，需要把「真实 IP」写进 /etc/hosts 才能连
  （与 github.com 同款处理）。apibay.org 是 Cloudflare 段 104.21.x，可：
      echo "<真实IP> apibay.org" | sudo tee -a /etc/hosts
  真实 IP 变化后重查一次即可（dig @223.5.5.5 apibay.org +short）。
- 搜索必须带浏览器 UA，否则 Cloudflare 直接 403。
"""
from __future__ import annotations

import logging
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# Cloudflare 会按 UA 拦 python-requests 的默认 UA，必须伪装浏览器
SEARCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

APIRAY_ENDPOINT = "https://apibay.org/q.php"

# 中文关键词自动翻译（apibay 收到中文 query 不搜索、只回全站热门榜，
# 表现就是「每次搜出来都一样」。翻成英文后再搜才有效）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"
_TRANS_LOCK = threading.Lock()
_TRANS_CACHE: dict = {}

# 中文片名 → 英文原名。**本地词典优先于 MyMemory**，因为免费翻译 API 对片名
# 返回的是「模糊历史匹配」而非真翻译，实测噪声极大，会直接变成错误搜索词：
#   流浪地球 → "g id Italic The Wandering Earth g Director Guo Fan"
#   星际穿越 → "Gold Interstellar"      你好李焕英 → "Hello Lee Hwan young"
# 这些结果喂给只吃英文的搜索引擎，表现就是「搜不出来 / 搜到的完全是别的片子」。
_CN_TO_EN = {
    # 华语
    "流浪地球": "The Wandering Earth", "流浪地球2": "The Wandering Earth II",
    "流浪地球3": "The Wandering Earth III",
    "让子弹飞": "Let the Bullets Fly", "你好李焕英": "Hi Mom",
    "长津湖": "The Battle at Lake Changjin", "战狼2": "Wolf Warrior 2",
    "红海行动": "Operation Red Sea", "哪吒之魔童降世": "Ne Zha",
    "我不是药神": "Dying to Survive", "卧虎藏龙": "Crouching Tiger Hidden Dragon",
    "霸王别姬": "Farewell My Concubine", "活着": "To Live",
    "大话西游": "A Chinese Odyssey", "功夫": "Kung Fu Hustle",
    "无间道": "Infernal Affairs", "重庆森林": "Chungking Express",
    "花样年华": "In the Mood for Love", "疯狂的石头": "Crazy Stone",
    "唐人街探案": "Detective Chinatown", "隐秘的角落": "The Bad Kids",
    "漫长的季节": "The Long Season", "狂飙": "The Knockout",
    "繁花": "Blossoms Shanghai", "庆余年": "Joy of Life", "三体": "Three-Body",
    "山海情": "Minning Town", "觉醒年代": "The Awakening Age",
    "英雄": "Hero", "新龙门客栈": "New Dragon Gate Inn",
    "甜蜜蜜": "Comrades Almost a Love Story", "春光乍泄": "Happy Together",
    "甲方乙方": "The Dream Factory", "手机": "Cell Phone",
    "集结号": "Assembly", "金陵十三钗": "The Flowers of War",
    "芳华": "Youth", "西红柿首富": "Hello Mr Billionaire",
    # 外语片常见译名
    "星际穿越": "Interstellar", "盗梦空间": "Inception", "蝙蝠侠": "Batman",
    "沙丘": "Dune", "沙丘2": "Dune Part Two", "奥本海默": "Oppenheimer",
    "泰坦尼克号": "Titanic", "阿凡达": "Avatar", "复仇者联盟": "The Avengers",
    "钢铁侠": "Iron Man", "蜘蛛侠": "Spider-Man", "超人": "Superman",
    "黑客帝国": "The Matrix", "终结者": "The Terminator", "异形": "Alien",
    "回到未来": "Back to the Future", "侏罗纪公园": "Jurassic Park",
    "权力的游戏": "Game of Thrones", "绝命毒师": "Breaking Bad",
    "黑镜": "Black Mirror", "怪奇物语": "Stranger Things",
    "西部世界": "Westworld", "生活大爆炸": "The Big Bang Theory",
    "老友记": "Friends", "行尸走肉": "The Walking Dead",
    "进击的巨人": "Attack on Titan", "鬼灭之刃": "Demon Slayer",
    "千与千寻": "Spirited Away", "龙猫": "My Neighbor Totoro",
    "天空之城": "Castle in the Sky", "你的名字": "Your Name",
    "寄生虫": "Parasite", "釜山行": "Train to Busan", "鱿鱼游戏": "Squid Game",
    "请回答1988": "Reply 1988", "来自星星的你": "My Love from the Star",
    "疯狂动物城": "Zootopia", "冰雪奇缘": "Frozen", "狮子王": "The Lion King",
    "飞屋环游记": "Up", "机器人总动员": "WALL-E", "寻梦环游记": "Coco",
    "心灵奇旅": "Soul", "头号玩家": "Ready Player One",
    "银翼杀手": "Blade Runner", "教父": "The Godfather",
    "肖申克的救赎": "The Shawshank Redemption", "阿甘正传": "Forrest Gump",
    "这个杀手不太冷": "Leon", "楚门的世界": "The Truman Show",
    "美丽人生": "Life Is Beautiful", "海上钢琴师": "The Legend of 1900",
    "搏击俱乐部": "Fight Club", "低俗小说": "Pulp Fiction",
    "沉默的羔羊": "The Silence of the Lambs", "七宗罪": "Se7en",
    "记忆碎片": "Memento", "致命魔术": "The Prestige",
    "黑暗骑士": "The Dark Knight", "信条": "Tenet", "敦刻尔克": "Dunkirk",
    "星际迷航": "Star Trek", "星球大战": "Star Wars",
    "加勒比海盗": "Pirates of the Caribbean", "速度与激情": "Fast and Furious",
    "碟中谍": "Mission Impossible", "夺宝奇兵": "Indiana Jones",
    "指环王": "The Lord of the Rings", "魔戒": "The Lord of the Rings",
    "霍比特人": "The Hobbit", "哈利波特": "Harry Potter",
    "疯狂的麦克斯": "Mad Max", "疯狂的麦克斯4": "Mad Max Fury Road",
    "壮志凌云": "Top Gun", "壮志凌云2": "Top Gun Maverick",
    "第一次的亲密接触": "The First Intimate Contact",
    # 日韩
    "东京物语": "Tokyo Story", "七武士": "Seven Samurai",
    "罗生门": "Rashomon", "情书": "Love Letter",
    "白色巨塔": "The Hospital", "蓝色生死恋": "Autumn in My Heart",
}

# MyMemory 返回的常夹带 HTML 标签残留与演职员表。遇到元信息词就截断，
# 再剔除格式噪声词，最后限制词数——避免把噪声当成片名的一部分去搜。
_META_CUT = re.compile(
    r"\b(director|directed|starring|star|cast|produced|producer|"
    r"writer|written|featuring|screenplay|based|novel)\b", re.I)
_NOISE_TOKENS = {
    "g", "id", "italic", "bold", "span", "div", "br", "html", "nbsp", "amp",
    "class", "style", "href", "src", "width", "height", "font", "color",
    "director", "directed", "starring", "cast", "produced", "writer", "written",
    "movie", "film",
    # 冠词（the/a/an）**不能**当噪声去掉——The Matrix / The Godfather 的
    # The 是片名的一部分，去掉反而搜不到。
}


def _clean_translation(text: str, max_words: int = 6) -> str:
    """把翻译结果清洗成「像片名」的词串。"""
    if not text:
        return ""
    m = _META_CUT.search(text)
    if m:
        text = text[:m.start()]
    words: list[str] = []
    for w in re.split(r"[^a-zA-Z0-9]+", text):
        if not w:
            continue
        # 单个字母（HTML 残留 g / b / i）一律丢弃；数字（如 2 / 007）保留
        if len(w) == 1 and not w.isdigit():
            continue
        if w.lower() in _NOISE_TOKENS:
            continue
        words.append(w)
        if len(words) >= max_words:
            break
    return " ".join(words)


@dataclass
class SearchHit:
    """一条搜索结果（磁力）。"""
    title: str
    size_bytes: int
    seeders: int
    magnet: str
    source: str = "apibay"

    @property
    def size_text(self) -> str:
        try:
            n = float(self.size_bytes or 0)
        except (TypeError, ValueError):
            return "-"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                if unit in ("GB", "TB"):
                    return "%.1f%s" % (n, unit)
                return "%d%s" % (int(n), unit)
            n /= 1024
        return "-"


def _proxies(proxy: str = "") -> Optional[dict]:
    p = (proxy or "").strip()
    return {"http": p, "https": p} if p else None


def _build_magnet(info_hash: str, name: str) -> str:
    dn = urllib.parse.quote(name or "")
    return "magnet:?xt=urn:btih:%s&dn=%s" % (info_hash.strip().upper(), dn)


def search_apibay(keyword: str, limit: int = 10, proxy: str = "",
                  timeout: int = 12) -> List[SearchHit]:
    """搜 The Pirate Bay（apibay.org 公开 API）。关键词中文无效时多半返回热门，需英文/原名。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    url = APIRAY_ENDPOINT + "?" + urllib.parse.urlencode({"q": kw, "cat": "0"})
    r = requests.get(url, headers={"User-Agent": SEARCH_UA},
                     proxies=_proxies(proxy), timeout=timeout)
    r.raise_for_status()
    try:
        rows = r.json()
    except ValueError:
        raise RuntimeError("搜索引擎返回了非 JSON（可能被拦），HTTP %s" % r.status_code)
    if not isinstance(rows, list):
        raise RuntimeError("搜索引擎返回格式异常")
    hits: List[SearchHit] = []
    for x in rows:
        name = str(x.get("name") or "").strip()
        info = str(x.get("info_hash") or "").strip()
        if not name or len(info) != 40:
            continue
        hits.append(SearchHit(
            title=name,
            size_bytes=int(x.get("size") or 0),
            seeders=int(x.get("seeders") or 0),
            magnet=_build_magnet(info, name),
            source="apibay",
        ))
        if len(hits) >= limit:
            break
    # 有做种者的排前面（广告/死种沉底）
    hits.sort(key=lambda h: h.seeders, reverse=True)
    return hits


def translate_cn_keyword(keyword: str, timeout: int = 6) -> str:
    """含中文的关键词先经免费翻译转英文，再喂给只吃英文的搜索引擎。

    实测 apibay 收到中文 query 时不真正搜索，而是返回全站热门榜（固定几条），
    表现就是「无论搜什么结果都一样」。中文片名翻成英文后命中率大幅提升
    （星际穿越 → Interstellar、流浪地球 → Wandering Earth 均可用）。

    翻译失败（网络/限流/无语料）时原样返回关键词，调用方自行兜底。
    结果带进程内缓存：同一关键词不重复请求翻译。
    """
    if not _CJK_RE.search(keyword or ""):
        return keyword
    kw = (keyword or "").strip()
    if not kw:
        return kw
    # ① 本地词典优先：网络翻译对片名极不可靠，能查到就不走网络
    local = _CN_TO_EN.get(kw)
    if local:
        return local
    with _TRANS_LOCK:
        cached = _TRANS_CACHE.get(kw)
    if cached is not None:
        return cached
    url = "%s?q=%s&langpair=zh-CN|en" % (
        MYMEMORY_ENDPOINT, urllib.parse.quote(kw))
    out = ""
    try:
        r = requests.get(url, headers={"User-Agent": SEARCH_UA},
                         timeout=timeout)
        r.raise_for_status()
        resp = r.json() or {}
        # 优先从 matches 数组里挑质量最高且含英文字母的条目，
        # 而非直接取 responseData.translatedText（该字段常取到低质量匹配）
        matches = resp.get("matches") or []
        best = None
        best_score = -1
        for m in matches:
            score = float(m.get("quality") or 0)
            match_val = float(m.get("match") or 0)
            combined = score * 1000 + match_val
            text = (m.get("translation") or "").strip()
            if text and any(c.isalpha() for c in text) and combined > best_score:
                best_score = combined
                best = text
        out = best or resp.get("responseData", {}).get("translatedText") or ""
    except Exception as exc:  # noqa: BLE001 - 翻译失败不影响主流程
        log.warning("中文关键词翻译失败（按原词搜索）: %s", exc)
    # ② 网络翻译兜底：必须清洗，否则 HTML 残留 / 演职员表会混进搜索词
    clean = _clean_translation(out)
    final = clean or kw
    with _TRANS_LOCK:
        _TRANS_CACHE[kw] = final
    if final != kw:
        log.info("中文关键词 %r → 翻译 %r 再搜索", kw, final)
    return final


# 可扩展引擎表：加新源时实现同名函数并注册进来
ENGINES = {
    "apibay": search_apibay,
}


def search_all(keyword: str, engines: Optional[List[str]] = None, limit: int = 8,
               proxy: str = "", timeout: int = 12) -> Tuple[List[SearchHit], List[str]]:
    """按启用的引擎列表搜索，合并结果（去重磁力），返回 (hits, errors)。

    errors 里是各引擎失败的简要原因，供调用方提示用户。
    """
    engines = engines or ["apibay"]
    # 搜索引擎（apibay）不支持中文：含中文关键词先翻译成英文再搜
    kw = translate_cn_keyword(keyword)
    hits: List[SearchHit] = []
    seen = set()
    errors: List[str] = []
    for name in engines:
        fn = ENGINES.get(name or "")
        if fn is None:
            errors.append("未知引擎 %r" % name)
            continue
        try:
            got = fn(kw, limit=limit * 2, proxy=proxy, timeout=timeout)
            for h in got:
                if h.magnet not in seen:
                    seen.add(h.magnet)
                    hits.append(h)
        except Exception as exc:  # noqa: BLE001 - 单个引擎失败不影响其它引擎
            log.warning("磁力引擎 %s 搜索失败: %s", name, exc)
            errors.append("%s: %s" % (name, str(exc)[:80]))
    hits.sort(key=lambda h: h.seeders, reverse=True)
    return hits[: max(1, limit)], errors


def to_payload(hits: List[SearchHit]) -> List[dict]:
    """转成 Telegram 机器人好用的纯数据（不携带 core 类型依赖）。"""
    return [{
        "title": h.title,
        "size_text": h.size_text,
        "size_bytes": h.size_bytes,
        "seeders": h.seeders,
        "magnet": h.magnet,
        "source": h.source,
    } for h in hits]
