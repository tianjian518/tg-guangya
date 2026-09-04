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
# 后缀允许点后跟字母（如 1992.x264 / 2023.BluRay），只防点后跟数字（版本号）
_YEAR = re.compile(r"(?<![\d.])(?:19|20)\d{2}(?!\d(?:\.\d+)?)")

# 分辨率 / 编码 / 封装 / 画质音轨（剥掉，不参与身份）
_TECH = re.compile(
    r"(?i)"
    r"\b(2160p|1440p|1080p|1080i|720p|480p|360p|4k|8k|uhd|fhd|hd|sdr)\b"
    r"|\b(blu[- ]?ray|bluray|bdrip|brrip|web[- ]?dl|webrip|webdl|remux|hdtv|hdrip|"
    r"dvdrip|dvdr|h264|h265|x264|x265|hevc|avc|mpeg|yuv420p)\b"
    r"|\b(remastered|restored|imax|hdr10?\+?|dolby\s*(?:vision|atmos|truehd)|dovi|dv|"
    r"truehd|dts[- ]?hd|dts[- ]?x|ac3|aac|flac|lpcm|5\.1|7\.1|10bit|8bit)\b"
    r"|dd5\.1|dts5\.1|eac3|e-\s*ac3|atmos|ma|mlp"
    r"|\.(mkv|mp4|avi|ts|rmvb|rm|iso|mov|wmv|flv|m2ts)(?=\s|$)"
)

# 中文噪声修饰词（语言 / 字幕 / 版本 / 广告词）
_NOISE_WORDS = re.compile(
    r"(高清|超清|中字|中英字幕|双语|国语|普通话|日语|韩语|英语|法语|泰语|粤语|台语|"
    r"简体|繁体|简中|繁中|字幕|无字幕|国配|中英|内嵌|外挂|"
    r"未删减|加长版|导演剪辑版|终极版|完整版|无水印|官方|预告|花絮|合集|特别篇|番外|"
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
_EP_CN = re.compile(r"第\s*([0-9一二两三四五六七八九十]+)\s*[集话話期]")        # 第X集/话/期
_EP_CN_BARE = re.compile(r"(?<![\d.])(\d{1,3})\s*[集话話期](?![\d.])")         # 18集（兜底）
_EP_EN = re.compile(r"(?i)\bep\.?\s*(\d{1,3})\b")                              # EP06

# 多集打包 / 聚合标记（出现即视为「包」，不生成单集签名，避免整包误杀后续单集）
_PACK = re.compile(
    r"全集|全\s*\d+\s*[集话話]|共\s*\d+\s*[集话話]|更新至|更新到|连载|连更|合集|"
    r"\d{1,3}\s*[-~至]\s*\d{1,3}\s*[集话話]|"                                  # 1-5集 / 1~5集 / 1至5集
    r"s\d{1,2}\s*[-~]\s*s?\d{1,2}\b|"                                          # S01-S02
    r"(?i)\bpart\s*\d+\s*(&|and|与)\s*(?:part\s*)?\d+\b",                       # Part 1 & 2 / Part I & II
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
    s = _YEAR.sub(" ", s)
    s = _SEP.sub(" ", s)
    s = _KEEP.findall(s)          # 再去掉空格标点等，只留字
    s = "".join(s).strip()
    if strip_trailing_nums:
        # 只去掉末尾 ≥3 位纯数字（"RandomMovie999"→"RandomMovie"），放过单/双位数（"JohnWick4"→不变）
        s = re.sub(r"[0-9]{3,}$", "", s).strip()
    return s


# 英文经典片名 → 官方中文片名映射。覆盖：好莱坞经典、港片、译制片、近年热门。
# 规则：key 去掉下划线/连字符/空格后的小写形式；value 是标准中文片名。
# 来源：豆瓣/TMDB 公开资料；不确定译名的不进，靠 TMDB API 兜底。
_EN_TO_CN = {
    # A
    "alien": "异形",
    "aliens": "异形2",
    "aladdin": "阿拉丁",
    "ameliapotts": "艾美莉娣·普特丝",
    "anastasia": "安娜斯塔西娅",
    "avatar": "阿凡达",
    "avengers": "复仇者联盟",
    # B
    "backtothefuture": "回到未来",
    "batman": "蝙蝠侠",
    "beautythebeast": "美女与野兽",
    "big": "大",
    "braveheart": "勇敢的心",
    # C
    "cash": "金钱本色",
    "castleinthesky": "天空之城",
    "chinatown": "唐人街",
    "classic": "经典",
    "closeencounters": "第三类接触",
    "crouchingtiger": "卧虎藏龙",
    "cyborg": "铁甲威龙",
    # D
    "danceswithwolves": "与狼共舞",
    "deadline": "生死时速",
    "diehard": "虎胆龙威",
    "diamondage": "钻石年代",
    "dragon": "龙",
    "dumbanddumber": "阿呆与阿瓜",
    # E
    "epic": "史诗",
    "eureka": "尤里卡",
    # F
    "fairlyoddparents": "梦幻奇缘",
    "fightclub": "搏击俱乐部",
    "frozen": "冰雪奇缘",
    "forestgump": "阿甘正传",
    "friday": "星期五",
    "fullmetal": "钢之炼金术师",
    # G
    "ghost": "人鬼情未了",
    "ghostbusters": "捉鬼敢死队",
    "godfather": "教父",
    "godzilla": "哥斯拉",
    "gladiator": "角斗士",
    "grease": "油脂",
    # H
    "harrypotter": "哈利·波特",
    "highlander": "高地人",
    "homealone": "小鬼当家",
    "hobbit": "霍比特人",
    "hook": "铁钩船长",
    "hostel": "恐怖Hostel",
    "hotshot": "雷霆壮志",
    "hudsuckproxy": "金钱堡垒",
    # I
    "infamous": "声名狼藉",
    "indianajones": "夺宝奇兵",
    "insane": "致命ID",
    "inception": "盗梦空间",
    "invisibleman": "隐形人",
    # J
    "jaws": "大白鲨",
    "jean": "让娜",
    "joseph": "约瑟夫",
    "judge": "判官",
    # K
    "kingkong": "金刚",
    "kungfu": "功夫",
    # L
    "lagrandebouffe": "法国大餐",
    "lilo": "星际宝贝",
    "lionking": "狮子王",
    "lost": "迷失",
    "lotr": "指环王",
    "lordoftherings": "指环王",
    "lucy": "露西",
    # M
    "matrix": "黑客帝国",
    "meninblack": "黑衣人",
    "minions": "小黄人",
    "misscongeniality": "特工佳丽",
    "moana": "海洋奇缘",
    "monstersinc": "怪兽电力公司",
    "moonalstronaut": "火星救援",
    "mrsdoubtfire": "窈窕奶爸",
    "mulan": "花木兰",
    # N
    "newdragon": "新龙门客栈",
    # O
    "oliver": "奥利弗",
    "one": "独白",
    "onthehouse": "房子",
    "oppoppy": "欧芭芭",
    "oppendheimer": "奥本海默",
    "outlander": "异乡人",
    # P
    "pacificrim": "环太平洋",
    "parasite": "寄生虫",
    "pan": "小飞侠",
    "pinkpanther": "粉红豹",
    "pirates": "加勒比海盗",
    "piratesofthecaribbean": "加勒比海盗",
    "planetearth": "地球脉动",
    "planetoftheapes": "决战猩球",
    "pooh": "小熊维尼",
    "poison": "毒药",
    "poltergeist": "鬼驱人",
    "pocahontas": "风中奇缘",
    "predator": "铁血战士",
    "princessbride": "公主新娘",
    "prom": "毕业舞会",
    # R
    "rambo": "第一滴血",
    "ratatouille": "美食总动员",
    "rebelwithoutacause": "无因的反叛",
    "reservoirdogs": "落水狗",
    "ring": "午夜凶铃",
    "rocky": "洛奇",
    "rosemary": "罗斯玛丽的婴儿",
    # S
    "scarface": "疤面煞星",
    "se7en": "七宗罪",
    "shining": "闪灵",
    "shooter": "生死狙击",
    "shrek": "怪物史瑞克",
    "signal": "信号",
    "silentstorm": "寂静风暴",
    "singletone": "单身男子",
    "skyfall": "大破天幕杀机",
    "scream": "惊声尖叫",
    "snowwhite": "白雪公主",
    "solace": "宁静",
    "southpark": "南方公园",
    "spiderman": "蜘蛛侠",
    "spirit": "灵魂战车",
    "starship": "星际战舰",
    "stargate": "星际之门",
    "starwars": "星球大战",
    "standbyme": "伴我同行",
    "stardust": "星尘",
    "strangerthanfiction": "荒诞剧团",
    "sunshine": "阳光小美女",
    # T
    "terminator": "终结者",
    "theater": "剧场",
    "thebossbaby": "宝贝老板",
    "thehungergames": "饥饿游戏",
    "themanfromuncle": "绅士密令",
    "thegrayman": "灰色绅士",
    "theinvisibleman": "隐形人",
    "thematrix": "黑客帝国",
    "thenightmarebeforechristmas": "圣诞夜惊魂",
    "thepacific": "太平洋战争",
    "thesimpsons": "辛普森一家",
    "thesuperman": "超人",
    "thetrumanshow": "楚门的世界",
    "thexfiles": "X档案",
    "tickettoheaven": "天堂门票",
    "timeaftertime": "时光倒流七十年",
    "tootsie": "杜丝先生",
    "totalrecall": "全面回忆",
    "toy": "玩具总动员",
    "transformers": "变形金刚",
    "transcendence": "超验骇客",
    "trap": "陷阱",
    "true": "真探",
    "tropicthunder": "热带惊雷",
    "twilight": "暮光之城",
    "topgun": "壮志凌云",
    # U
    "uforchestra": "UFO交响曲",
    "up": "飞屋环游记",
    # V
    "vacancy": "空房惊魂",
    "valerian": "星际特工",
        "vanhelsing": "范海辛",
    "vendetta": "V字仇杀队",
        "vforvendetta": "V字仇杀队",
    # W
    "walle": "机器人总动员",
    "war": "战争",
    "wardrobe": "纳尼亚传奇",
    "water": "少年派的奇幻漂流",
    "watchmen": "守望者",
    "whiplash": "爆裂鼓手",
    "wild": "荒野猎人",
    "walk": "行走",
    "wallace": "超级无敌掌门狗",
    "warhorse": "战马",
    "white": "白骑士",
    "wolf": "狼来了",
    "winnie": "小熊维尼",
    "wizard": "绿野仙踪",
    # X
    "xmen": "X战警",
    # Y
    "yoda": "尤达",
    # Z
    "zodiac": "十二宫",
    # 热门剧集
    "breakingbad": "绝命毒师",
    "gameofthrones": "权力的游戏",
    "strangerthings": "怪奇物语",
    "westworld": "西部世界",
    "thewalkingdead": "行尸走肉",
    "houseofcards": "纸牌屋",
    "sherlock": "神探夏洛克",
    "blackmirror": "黑镜",
    "thesopranos": "黑道家族",
    "friends": "老友记",
        "howmetyourmother": "老爸老妈浪漫史",
    "thebigbangtheory": "生活大爆炸",
    "supernatural": "邪恶力量",
    "lost": "迷失",
    "prisonbreak": "越狱",
    "bitten": "妖女迷行",
    "thedailyshow": "每日秀",
    "southpark": "南方公园",
    # 科幻/动作补充
    "jurassicpark": "侏罗纪公园",
    "jurassicken": "侏罗纪世界",
    "interstellar": "星际穿越",
    "gravity": "地心引力",
    "mashine": "机器纪元",
    "dredd": "特警判官",
    "edgeoftomorrow": "明日边缘",
    "livefreeordiethard": "虎胆龙威4",
    "diehard4": "虎胆龙威4",
    "madmax": "疯狂的麦克斯",
    "madmaxfuryroad": "疯狂的麦克斯：狂暴之路",
    "bladerunner": "银翼杀手",
    "blade2": "银翼杀手2",
    "blade3": "银翼杀手3",
    "thematrixresurrections": "黑客帝国4",
    "thematrixrevolutions": "黑客帝国3",
    "thematrixreloaded": "黑客帝国2",
    "theprestige": "致命魔术",
    "theparallaxview": "暗杀谱",
    "theconspiracy": "阴谋",
    "thehungergames": "饥饿游戏",
    "catchingfire": "饥饿游戏2",
    "mockingjay": "饥饿游戏3",
    # 漫威
    "ironman": "钢铁侠",
    "ironman2": "钢铁侠2",
    "ironman3": "钢铁侠3",
    "thor": "雷神",
    "thortheruggedworld": "雷神2",
        "thorRagnarok": "雷神3",
    "captainamerica": "美国队长",
        "captainamericawintersoldier": "美国队长2",
    "captainamericacivilwar": "美国队长3",
        "guardiansofthegalaxy": "银河护卫队",
        "guardiansofthegalaxyvol2": "银河护卫队2",
        "guardiansofthegalaxyvol3": "银河护卫队3",
    "antman": "蚁人",
        "antmanandthewasp": "蚁人2",
        "doctorstrange": "奇异博士",
        "blackpanther": "黑豹",
        "avengersinfinitywar": "复仇者联盟3",
        "avengersendgame": "复仇者联盟4",
        "avengersageofultron": "复仇者联盟2",
    "thanos": "灭霸",
        "spidermanhomecoming": "蜘蛛侠：英雄归来",
        "spidermanfarfromhome": "蜘蛛侠：英雄远征",
        "spidermannowayhome": "蜘蛛侠：英雄无归",
        "spidermanacrossthespiderverse": "蜘蛛侠：纵横宇宙",
        "spidermannotisback": "蜘蛛侠：归来",
    "venom": "毒液",
        "venomlettherebecarnage": "毒液2",
    # DC
    "batmanbegins": "蝙蝠侠：侠影之谜",
        "themdarkknight": "蝙蝠侠：黑暗骑士",
        "thedarkknightrises": "蝙蝠侠：黑暗骑士崛起",
        "manofsteel": "超人：钢铁之躯",
    "bvs": "蝙蝠侠大战超人",
        "justiceleague": "正义联盟",
        "wonderwoman": "神奇女侠",
    "aquaman": "海王",
    "shazam": "沙赞",
    "constellation": "星座",
    # 动画
        "toystory": "玩具总动员",
    "findingnemo": "海底总动员",
    "findingdorothy": "寻找多莉",
    "ratatouille": "美食总动员",
        "walle": "机器人总动员",
    "cars": "赛车总动员",
    "insideout": "头脑特工队",
    "insideout2": "头脑特工队2",
    "coco": "寻梦环游记",
    "up": "飞屋环游记",
        "monstersuniversity": "怪兽大学",
    "brave": "勇敢传说",
    "frozen": "冰雪奇缘",
    "frozen2": "冰雪奇缘2",
    "zootopia": "疯狂动物城",
    "moana": "海洋奇缘",
        "wreckitralph": "无敌破坏王",
        "bighero6": "超能陆战队",
        "howtotrainyourdragon": "驯龙高手",
        "howtotrainyourdragon2": "驯龙高手2",
        "howtotrainyourdragon3": "驯龙高手3",
        "kungfupanda": "功夫熊猫",
        "kungfupanda2": "功夫熊猫2",
        "kungfupanda3": "功夫熊猫3",
    "shrek": "怪物史瑞克",
    "shrek2": "怪物史瑞克2",
        "shrekthethird": "怪物史瑞克3",
        "shrekforeverafter": "怪物史瑞克4",
        "iceage": "冰河世纪",
        "iceage2": "冰河世纪2",
        "iceage3": "冰河世纪3",
        "iceage4": "冰河世纪4",
        "iceage5": "冰河世纪5",
        "howtotrainyourdragon": "驯龙高手",
        "despicableme": "神偷奶爸",
        "despicableme2": "神偷奶爸2",
        "despicableme3": "神偷奶爸3",
    "minions": "小黄人",
    "sing": "欢乐好声音",
    "sing2": "欢乐好声音2",
    "home": "家园反攻",
    "megamind": "超级大坏蛋",
    "klaus": "克劳斯",
    "wonder": "奇迹男孩",
        "thelionking": "狮子王",
        "thelionking2": "狮子王2",
        "thelionking3": "狮子王3",
    "aladdin": "阿拉丁",
    "aladdin2": "阿拉丁2",
    "moana": "海洋奇缘",
    "tangled": "魔发奇缘",
    "frozen": "冰雪奇缘",
    "brave": "勇敢传说",
    "onward": "1/2的魔法",
    "soul": "心灵奇旅",
    "encanto": "魔法满屋",
    "red": "红杉",
    "elemental": "元素方城市",
    # 热门续作
        "fastandfurious": "速度与激情",
        "fastfive": "速度与激情5",
        "fastsix": "速度与激情6",
        "fastseven": "速度与激情7",
        "fasteight": "速度与激情8",
        "fastnine": "速度与激情9",
        "fastx": "速度与激情10",
        "fastandfurioushobbs&shaw": "速度与激情：特别行动",
        "furious7": "速度与激情7",
        "xmen": "X战警",
        "xmen2": "X战警2",
        "xmenthelaststand": "X战警3",
        "xmendaysoffuturepast": "X战警：逆转未来",
        "xmenapocalypse": "X战警：天启",
        "xmendarkphoenix": "X战警：黑凤凰",
    "logan": "罗根",
    "deadpool": "死侍",
    "deadpool2": "死侍2",
        "deadpool&wolverine": "死侍与金刚狼",
    "wolverine": "金刚狼",
        "wolverinetheimmortal": "金刚狼：永生",
    # 经典港片
        "abettertomorrow": "英雄本色",
        "abettertomorrow2": "英雄本色2",
        "abettertomorrow3": "英雄本色3",
        "onceuponatimeinchina": "黄飞鸿",
        "onceuponatimeinchina2": "黄飞鸿2",
        "onceuponatimeinchina3": "黄飞鸿3",
        "onceuponatimeinchina4": "黄飞鸿4",
        "drunkenmaster": "醉拳",
        "drunkenmaster2": "醉拳2",
        "fistoflegend": "精武门",
        "enterthedragon": "龙争虎斗",
        "gameofdeath": "死亡游戏",
    "bloodsport": "龙争虎斗",
        "killingquest": "龙兄虎弟",
        "projecta": "A计划",
        "projecta2": "A计划2",
        "wang'sfight": "龙拳",
        "fistoffury": "精武门",
    # 周星驰经典
    "chineseodyssey": "大话西游",
    "chineseodisseypart1": "大话西游之月光宝盒",
    "chineseodisseypart2": "大话西游之大圣娶亲",
    "chineseodyssey2": "大话西游2",
    # 近五年热门（2020-2026）
    "dune": "沙丘",
    "dunepart2": "沙丘2",
    "duneparttwo": "沙丘2",
        "everythingeverywhereallatonce": "瞬息全宇宙",
        "topgunmaverick": "壮志凌云2",
        "notimetodie": "007：无暇赴死",
        "blackadam": "黑亚当",
        "themenu": "菜单惊魂",
        "glassonion": "利刃出鞘2",
        "bullettrain": "子弹列车",
    "ambulance": "亡命救护车",
        "thebatman": "新蝙蝠侠",
    "thewanderingearth": "流浪地球",
    "thewanderingearth2": "流浪地球2",
    "avatar2": "阿凡达2",
        "avatarthewayofwater": "阿凡达2",
        "blackpantherwakandaforever": "黑豹2",
        "thebatman": "新蝙蝠侠",
        "theflash": "闪电侠",
        "bluebeetle": "蓝甲虫",
        "creediii": "奎迪3",
        "johnwick": "疾速追杀",
        "johnwick2": "疾速追杀2",
        "johnwick3": "疾速追杀3",
        "johnwick4": "疾速追杀4",
    "extraction": "惊天营救",
    "extraction2": "惊天营救2",
        "thegrayman": "灰影人",
        "themother": "母亲",
        "theoldguard": "永生守卫",
    "sugar": "糖",
        "theAdamProject": "亚当计划",
        "don'tlookup": "不要抬头",
        "tickettoparadise": "天堂门票",
        "thebull": "公牛",
        "thelostcity": "迷失之城",
        "the355": "355",
    "thunderbolts": "雷霆特工队",
        "theelectricstate": "电子县",
        "thewildrobot": "荒野机器人",
        "insideout2": "头脑特工队2",
    "moana2": "海洋奇缘2",
        "despicableme4": "神偷奶爸4",
        "knightofcups": "纸月",
        "thefallguy": "特技狂人",
        "badboysforlife": "绝地战警3",
        "badboysrideordie": "绝地战警4",
    "f9": "速度与激情9",
    "f10": "速度与激情10",
        "fastx": "速度与激情10",
        "thesuperMariobrosmovie": "超级马里奥兄弟电影版",
        "pussinboots": "穿靴子的猫",
        "pussinbootsthelastwish": "穿靴子的猫2",
        "teenagemutantninjaturtles": "忍者神龟",
        "teenagemutantninjaturtlesmutantmayhem": "忍者神龟：变种大乱斗",
        "spiesindisguise": "间谍之狼",
    "wish": "星愿",
        "thenutcrackerandthefourrealms": "胡桃夹子与四个王国",
    "encanto": "魔法满屋",
    "luca": "卢卡",
        "turningred": "青春变形记",
    "ellarie": "夏日友晴天",
    "elemental": "疯狂元素城",
    "if": "如果",
        "thebadguys": "坏蛋联盟",
        "thebadguys2": "坏蛋联盟2",
    "migration": "智能大反攻",
        "robotdreams": "机器人之梦",
        "theboyandtheheron": "你想活出怎样的人生",
    "maestro": "音乐大师",
    "nyad": "奈德",
        "thezoneofinterest": "利益区域",
        "pastLives": "过往人生",
        "theholdovers": "留校察看",
        "americanfiction": "美国小说",
    "anora": "阿诺拉",
    "challengers": "挑战者",
        "theZoneofInterest": "利益区域",
        "poorthings": "可怜的东西",
        "theholdovers": "留校察看",
    "oppenheimer": "奥本海默",
    "barbie": "芭比",
        "killersoftheflowermoon": "花月杀手",
        "thecolorpurple": "紫色",
    "napoleon": "拿破仑",
    "wildeabe": "角斗士2",
    "gladiator2": "角斗士2",
    "wonka": "旺卡",
        "theironClaw": "铁爪",
        "flymetothemoon": "带我去月球",
        "meangirls": "贱女孩",
    "saltburn": "盐镇",
        "societyofthesnow": "雪国列车",
        "themonkey": "猴子",
        "evildeadrise": " evil dead: 崛起",
        "sawxi": "电锯惊魂11",
        "fivenightsatfreddy's": "玩具熊的五夜后宫",
        "thecreator": "创：战神",
        "alien:covenant": "异形：契约",
        "alien:romulus": "异形：夺命舰",
        "thelastofus": "最后生还者",
        "houseofthedragon": "龙之家族",
        "thelastofuss01": "最后生还者",
    "severance": "人生切割术",
    "fallout": "辐射",
        "3bodyproblem": "三体",
        "thethreebodyproblem": "三体",
    "shōgun": "幕府将军",
        "thebear": "熊家餐馆",
        "slowhorses": "慢马",
    "beef": "怒呛人生",
        "truedetective": "真探",
        "truedetectivenightcountry": "真探：夜之地",
    "house": "豪斯医生",
        "thewire": "火线",
        "thesopranos": "黑道家族",
    "deadwood": "死木",
        "boardwalkempire": "大西洋帝国",
    "vinyl": "黑胶岁月",
    "peacemaker": "和平精英",
        "thelastofus": "最后生还者",
    "fallout": "辐射",
        "3bodyproblem": "三体",
        "shadowandbone": "暗影与骨",
        "thewheeloftime": "时光之轮",
        "Hisdarkmaterials": "黑暗物质",
    "foundation": "基地",
        "theexpanse": "苍穹浩瀚",
        "forallmankind": "为全人类",
        "siliconvalley": "硅谷",
        "thesocialnetwork": "社交网络",
        "thepatent": "专利",
        "theinnovators": "创新者",
        "thecode": "代码",
        "thehack": "黑客",
        "theglitch": "故障",
        "thecrash": "崩溃",
        "thebloom": "开花",
        "thedrop": "坠落",
        "thelift": "提升",
        "thefall": "坠落",
        "therise": "崛起",
        "theend": "终点",
        "thebeginning": "开始",
        "themiddle": "中间",
        "thetop": "顶端",
        "thebottom": "底部",
        "theedge": "边缘",
        "thecenter": "中心",
        "theside": "侧面",
        "thefront": "前端",
        "theback": "后端",
        "theleft": "左侧",
        "theright": "右侧",
        "thenow": "现在",
        "thethen": "那时",
        "thehere": "这里",
        "thethere": "那里",
        "thewho": "谁",
        "thewhat": "什么",
        "thewhere": "哪里",
        "thewhen": "何时",
        "thewhy": "为什么",
        "thehow": "如何",
}

# 额外关键词合并映射（标题里含这些英文短语 → 已知中文）
# 优先级低于上面的完整映射；用于处理标题片段化匹配的情况
# key 里的空格会在匹配时去掉，所以写法和去掉空格后都能匹配
_EN_PHRASES = {
    "dragon gate": "新龙门客栈",
    "new dragon gate": "新龙门客栈",
    "dragon inn": "新龙门客栈",
    "crouching tiger hidden dragon": "卧虎藏龙",
    "kung fu classics": "功夫经典",
    "kung fu": "功夫",
    "fist of legend": "精武门",
    "fist": "精武门",
    "bloodsport": "龙争虎斗",
    "game of death": "死亡游戏",
    "enter the dragon": "龙争虎斗",
    "inception": "盗梦空间",
    "matrix": "黑客帝国",
    "avatar": "阿凡达",
    "titanic": "泰坦尼克号",
    "star wars": "星球大战",
    "fight club": "搏击俱乐部",
    "pulp fiction": "低俗小说",
    "godfather": "教父",
    "forrest gump": "阿甘正传",
    "interstellar": "星际穿越",
    "gladiator": "角斗士",
    "the dark knight": "蝙蝠侠：黑暗骑士",
    "chinese odyssey": "大话西游",
    "a chinese odyssey": "大话西游",
}


def _normalize_phrase(s: str) -> str:
    """去掉空格/连字符/点，用于在已归一化的 core 里做模糊匹配。"""
    return re.sub(r"[\s\-\._]+", "", s.lower())


# 预归一化的短语表，避免每次重复计算
_NORM_PHRASES = [(k, v) for k, v in _EN_PHRASES.items()]


def _try_tmdb_translate(core: str) -> str:
    """通过 TMDB API 查询英文片名的中文名（本地字典拿不到的兜底方案）。
    只在 config.tmdb.api_key 已配置时生效，其他情况静默失败。"""
    from core.config import AppConfig
    from core.data_dir import resolve_config_path
    cfg = AppConfig.load(str(resolve_config_path()))
    if not core or not cfg.tmdb.api_key:
        return ""
    # 剥掉年份和无关数字，取前 50 字符作为搜索词
    search = re.sub(r'\b\d{4}\b', '', core)[:50].strip()
    if not search or len(search) < 2:
        return ""
    try:
        url = (
            f"https://api.themoviedb.org/3/search/movie?query={urllib.parse.quote(search)}"
            f"&language=zh-CN&api_key={cfg.tmdb.api_key}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "tg-guangya/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        if results and results[0].get("title"):
            return results[0]["title"]
    except Exception:
        pass
    return ""


def _try_translate_core(core: str) -> str:
    """尝试把英文核心片名翻译成中文。
    优先级：本地字典 → TMDB API → 空字符串（表示无法翻译，保留原名）。"""
    if not core:
        return ""
    # 先剥掉 Sxx / SxxExx 等集数标记（strip_noise 会保留数字，需主动去掉）
    # (?<=[a-zA-Z]) 确保 S 前面是字母（处理 "BreakingBadS01" 这种粘连情况）
    core_clean = re.sub(r'(?i)(?<=[a-zA-Z])s\d{1,2}(?=$|[^a-zA-Z0-9])', '', core)
    core_clean = re.sub(r'(?i)\b(?:s\d{1,2}e\d{1,2}|ep\s*\d+)\b', '', core_clean)
    # 剥掉 "Part 1 & 2" / "Part1&2" / "Part12MA" 等多部分电影噪声
    # 先处理有空格的原版（Part 1 & 2），再处理粘连版（Part12MA）
    core_clean = re.sub(r'(?i)\bpart\s*\d+\s*(&|and|与)\s*(?:part\s*)?\d+\b', ' ', core_clean)
    core_clean = re.sub(r'(?i)part\d+', '', core_clean)  # Part1 / Part12 / PartII 等（不含\b，避免粘连情况）
    core_clean = core_clean.strip()
    low = core_clean.lower().replace("_", "").replace("-", "").replace(" ", "")
    if not low:
        return ""
    # 1. 精确匹配字典
    if low in _EN_TO_CN:
        return _EN_TO_CN[low]
    # 2. 关键词匹配（标题片段形式）
    for phrase, cn in _NORM_PHRASES:
        if _normalize_phrase(phrase) in low:
            return cn
    # 3. TMDB 兜底（需配 api_key）
    translated = _try_tmdb_translate(core_clean)
    if translated:
        return translated
    return ""


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
    # 纯英文核心：先剥尾部数字再翻译（避免 "RandomMovie999" 之类误判），然后尝试映射表翻译
    if not re.search(r"[一-鿿]", info.core or ""):
        info.core = _strip_noise(info.core, strip_trailing_nums=True)
        translated = _try_translate_core(info.core)
        if translated:
            info.core = translated
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
