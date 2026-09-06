"""磁力搜索引擎（供 TG 机器人 /s 搜索用）。

机器人能直接搜「全网磁力种子」而不是等频道更新：`/s 片名` 返回一批种子，
点按钮或发链接即可一键转存光鸭。搜索发生在程序运行的这台机器上。

引擎（2026-09 实测样本驱动，样本存 tests/fixtures/）：
  apibay  The Pirate Bay 公开 API（电影/欧美为主）。**不支持中文**：
          收到中文 query 不搜索、只回全站热门榜（表现就是「每次搜出来
          都一样」），必须翻成英文再搜。
          GET https://apibay.org/q.php?q=<关键词>&cat=0  →  JSON 数组
  nyaa    nyaa.si RSS（动漫/剧集全类，**直接吃中文**：斗破苍穹/庆余年等
          国产剧国漫都能搜到，日韩更不用说）——中文资源短板的主要补充。
          GET https://nyaa.si/?page=rss&q=<关键词>  →  RSS XML
          样本实测：中文「斗破苍穹」返回 75 条、最新集次日即有。

注意（部署这台机器的同学）：
- 搜索引擎域名一般被污染/封锁，需要把「真实 IP」写进 /etc/hosts 才能连
  （与 github.com 同款处理）。可用阿里 DoH 查真实 IP（1.1.1.1/8.8.8.8 的
  DoH 同样被拦，223.5.5.5 是逃生口）：
      curl "https://223.5.5.5/resolve?name=apibay.org&type=A"  # JSON 里的 Answer
      curl "https://223.5.5.5/resolve?name=nyaa.si&type=A"
      echo "<真实IP> apibay.org" | sudo tee -a /etc/hosts
- 搜索必须带浏览器 UA，否则 Cloudflare 直接 403。
- nyaa.si 也可走 config 的 bot.proxy。
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
    # 近三年爆款电影
    "哪吒之魔童闹海": "Ne Zha 2", "哪吒2": "Ne Zha 2",
    "封神第一部": "Creation of the Gods I", "封神第二部": "Creation of the Gods II",
    "热辣滚烫": "YOLO", "消失的她": "Lost in the Stars",
    "孤注一掷": "No More Bets", "满江红": "Full River Red",
    "坚如磐石": "Under the Light", "年会不能停": "Johnny Keep Walking",
    "飞驰人生": "Pegasus", "飞驰人生2": "Pegasus 2",
    "长安三万里": "Chang An", "深海": "Deep Sea",
    "第二十条": "Article 20", "三大队": "Endless Journey",
    "少年的你": "Better Days", "送你一朵小红花": "A Little Red Flower",
    "人生大事": "Lighting Up the Stars", "独行月球": "Moon Man",
    "刺杀小说家": "A Writer's Odyssey", "雄狮少年": "I Am What I Am",
    "白蛇2青蛇劫起": "Green Snake", "误杀": "Sheep Without a Shepherd",
    "志愿军": "The Volunteers", "好东西": "Her Story",
    # 国漫（nyaa/种子站常用罗马化名）
    "斗破苍穹": "Battle Through the Heavens", "斗罗大陆": "Soul Land",
    "凡人修仙传": "A Record of a Mortal's Journey to Immortality",
    "仙逆": "Renegade Immortal", "完美世界": "Perfect World",
    "吞噬星空": "Swallowed Star", "遮天": "Shrouding the Heavens",
    "诛仙": "Jade Dynasty", "少年歌行": "Great Journey of Teenagers",
    # 热门剧集
    "庆余年第二季": "Joy of Life 2", "莲花楼": "Mysterious Lotus Casebook",
    "长相思": "Lost You Forever", "与凤行": "The Legend of Shen Li",
    "墨雨云间": "The Double", "大奉打更人": "Guardians of the Dafeng",
    "苍兰诀": "Love Between Fairy and Devil", "星汉灿烂": "Love Like the Galaxy",
    "陈情令": "The Untamed", "琅琊榜": "Nirvana in Fire",
    "甄嬛传": "Empresses in the Palace", "知否知否应是绿肥红瘦": "The Story of Ming Lan",
    "都挺好": "All Is Well", "小欢喜": "A Little Reunion",
    "白鹿原": "White Deer Plain", "人世间": "A Lifelong Journey",
    "沉默的真相": "The Long Night", "无证之罪": "Burning Ice",
    "白夜追凶": "Day and Night", "扫黑风暴": "Crime Crackdown",
    "去有风的地方": "Meet Yourself", "琉璃": "Love and Redemption",
    "山河令": "Word of Honor", "一念永恒": "A Will Eternal",
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


# ---------------- Nyaa（动漫/剧集全类，支持中文原词）----------------

NYAA_ENDPOINT = "https://nyaa.si/"
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_FIELD_RE = {f: re.compile(r"<%s>(.*?)</%s>" % (f, f), re.S)
             for f in ("title", "nyaa:infoHash", "nyaa:seeders", "nyaa:size")}
_UNIT_BYTES = {"B": 1, "KB": 10**3, "KiB": 1 << 10, "MB": 10**6, "MiB": 1 << 20,
               "GB": 10**9, "GiB": 1 << 30, "TB": 10**12, "TiB": 1 << 40}


def _parse_ib_size(text: str) -> int:
    """「1.2 GiB」→ 字节数；解析不出返回 0（只影响展示排序，不致命）。"""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", (text or "").strip())
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNIT_BYTES.get(m.group(2), 0))
    except ValueError:
        return 0


def search_nyaa(keyword: str, limit: int = 10, proxy: str = "",
                timeout: int = 12) -> List[SearchHit]:
    """搜 nyaa.si RSS（动漫/剧集为主，中文/日韩资源覆盖远好于 TPB）。

    直接吃中文原词（真实样本实测：中文「斗破苍穹」75 条、「庆余年」有结果）。
    RSS 条目带 nyaa:infoHash / nyaa:seeders / nyaa:size，可直接构磁力。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    url = NYAA_ENDPOINT + "?" + urllib.parse.urlencode(
        {"page": "rss", "q": kw, "f": "0", "c": "0_0"})
    r = requests.get(url, headers={"User-Agent": SEARCH_UA},
                     proxies=_proxies(proxy), timeout=timeout)
    r.raise_for_status()
    body = r.text or ""
    if "<item>" not in body:
        # nyaa 被Cloudflare 拦时返回 HTML 挑战页；正常空结果也有 RSS 头
        raise RuntimeError("Nyaa 返回非 RSS（可能被 Cloudflare 拦），HTTP %s" % r.status_code)
    hits: List[SearchHit] = []
    for chunk in _ITEM_RE.findall(body):
        def g(tag: str) -> str:
            m = _FIELD_RE[tag].search(chunk)
            return (m.group(1).strip() if m else "")
        title = g("title")
        info = g("nyaa:infoHash")
        if not title or len(info) != 40:
            continue
        hits.append(SearchHit(
            title=title,
            size_bytes=_parse_ib_size(g("nyaa:size")),
            seeders=int(g("nyaa:seeders") or 0),
            magnet=_build_magnet(info, title),
            source="nyaa",
        ))
        if len(hits) >= limit:
            break
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


# 可扩展引擎表：加新源时实现同名函数并注册进来。
# CJK_OK = 该引擎直接支持中文关键词（不走翻译）。
#   apibay 不支持中文（中文 query 只回热门榜），必须先翻成英文；
#   nyaa 直接吃中文（国漫/国产剧/日韩收录好，且用户看到的标题里本就有中文）。
ENGINES = {
    "apibay": search_apibay,
    "nyaa": search_nyaa,
}
CJK_OK_ENGINES = {"nyaa"}


def search_all(keyword: str, engines: Optional[List[str]] = None, limit: int = 8,
               proxy: str = "", timeout: int = 12) -> Tuple[List[SearchHit], List[str]]:
    """按启用的引擎列表搜索，合并结果（去重磁力），返回 (hits, errors)。

    errors 里是各引擎失败的简要原因，供调用方提示用户。
    中文关键词：支持中文的引擎（nyaa）用原词直搜；只吃英文的引擎（apibay）
    先经本地词典/翻译转英文——分引擎处理，避免「为 nyaa 也翻一遍」既浪费
    翻译请求，又把中文剧名翻成错误英文导致 nyaa 搜偏。
    """
    engines = engines or list(ENGINES)
    kw_raw = (keyword or "").strip()
    needs_translation = any(e not in CJK_OK_ENGINES for e in engines) \
        and bool(_CJK_RE.search(kw_raw))
    kw_translated = translate_cn_keyword(kw_raw) if needs_translation else kw_raw
    hits: List[SearchHit] = []
    seen = set()
    errors: List[str] = []
    for name in engines:
        fn = ENGINES.get(name or "")
        if fn is None:
            errors.append("未知引擎 %r" % name)
            continue
        kw = kw_raw if name in CJK_OK_ENGINES else kw_translated
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
