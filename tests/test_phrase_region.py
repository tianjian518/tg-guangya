"""短语地区识别回归测试。

复现「华语电影通通分到欧美电影」的根因修复：
lookup 的短语分支原本查 BUILTIN_REGION.get(phrase)，但 phrase 是归一化短语串，
而 BUILTIN_REGION 的 key 是精确字典 key，两者对不上 → 短语命中时地区恒为空 →
分类器按「原标题 0% 中文」兜底判欧美。补录 PHRASE_REGION 后应正确给出 cn/jpkr。

另外本测试还顺手钉住了几处字典把华语片错标成 west 的真实 bug
（himom / mypeoplemycountry / thebattleatlakechangjin 等），修复后一并守住。

断言聚焦「地区词」：目录名里必须出现 华语 / 日韩 / 欧美。剧/影的细分层级
（无集数标记时韩剧易被判成电影）属于分类器另一类已知歧义，不在本次修复范围。

直接跑：python3 tests/test_phrase_region.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._helpers import build_resolver, pick_category


def _check(title: str, expected_region: str):
    """单条断言：解析 + 分类 + resolve 全链路，地区必须判对。

    真实流程里 ident 会先剥掉年份/编码/压制组噪声，再用干净 core 去查字典，
    所以要看最终链路里的 region_hint，而不是拿原始噪杂标题直接查 lookup。
    """
    _, clf, resolver, _ = build_resolver()
    got = pick_category(title, clf, resolver)
    assert got["region_hint"] == expected_region, (
        f"pick_category({title!r}).region_hint = {got['region_hint']!r}，期望 {expected_region!r}\n"
        f"  folder={got['folder']!r} category={got['category']!r}")
    # 精准守住原始缺陷「华语/日韩被误分欧美」：地区不是欧美的片，目录名绝不能是「欧美」。
    # （动漫类目录叫「日本动漫」、韩剧叫「日韩剧」，具体字眼不一，但都不该是欧美）
    assert "欧美" not in got["category"], (
        f"pick_category({title!r}).category = {got['category']!r} 含「欧美」——"
        f"非欧美片被误判到欧美（原始缺陷复现）")
    print(f"  OK {title[:44]:<46} → {got['category']:<8} (region_hint={got['region_hint']!r})")


def test_phrase_region_cn():
    print("\n=== 短语地区：华语 / 港片（无中文标题也应判华语）===")
    cases = [
        "New Dragon Gate Inn [1992] 1080p BluRay",
        "Crouching Tiger Hidden Dragon 2000 2160p",
        "Enter The Dragon 1973 1080p",
        "Fist Of Fury 1972 4K",
        "Once Upon A Time In China 1991",
        "A Chinese Odyssey 1995 1080p",
        "Kung Fu Hustle 2004 2160p",
        "Shaolin Soccer 2001",
        "The Wandering Earth 2019 4K",
        "Creation Of The Gods 2023 1080p",
        "The Battle At Lake Changjin 2021",
        "My People My Country 2019",
        "Hi Mom 2021 2160p",
        "Cold.War.1994.2026.1080p.HiveWeb",
    ]
    for title in cases:
        _check(title, "cn")


def test_phrase_region_jpkr():
    print("\n=== 短语地区：日韩（应判日韩，不再是欧美）===")
    cases = [
        "Squid Game Season 1 1080p WEB-DL",
        "Crash Landing On You 2019 1080p",
        "All Of Us Are Dead 2022 2160p",
        "Extraordinary Attorney Woo 2022",
        "Demon Slayer 2020 4K",
        "Spy Family 2022 1080p",
        "My Neighbor Totoro 1988 1080p",
        "Howl's Moving Castle 2004 2160p",
        "The Boy And The Heron 2023",
        "The World Of The Married 2020",
    ]
    for title in cases:
        _check(title, "jpkr")


def test_chinese_title_always_cn():
    print("\n=== 中文标题影片（自带中文，地区提示应为 cn 且强提示）===")
    cases = [
        "周星驰大话西游系列 1080p",
        "流浪地球2 2023 4K 国语中字",
        "庆余年 第6集 2160p 中字",
        "繁花 更新至18集 1080P 国语中字",
    ]
    for title in cases:
        _check(title, "cn")


def test_chinese_title_tmdb_region():
    """中文译名（无夹带英文原名）应按 TMDB 的 original_language 判定地区，
    而非一律弱判华语。复现「耳语人 (2026) 落进华语电影」缺陷。

    用 mock 模拟 TMDB 命中，验证 ident 的中文 core 现在也会去查地区。
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from unittest import mock

    from core import media_meta as mm
    from core.ident import analyze

    print("\n=== 中文译名经 TMDB 地区识别（非一律华语）===")
    table = {"耳语人": "en", "名侦探柯南": "ja", "霸王别姬": "zh"}
    def fake_lookup(q, year=0):
        lang = table.get(q)
        return {"original_language": lang} if lang else None

    with mock.patch.object(mm, "_tmdb_key", lambda: "fake-key"), \
         mock.patch.object(mm, "_tmdb_lookup", fake_lookup):
        assert analyze("耳语人 (2026)").region_hint == "west", "美国片译名应判 west"
        assert analyze("名侦探柯南 (1997)").region_hint == "jpkr", "日本片译名应判 jpkr"
        assert analyze("霸王别姬 (1993)").region_hint == "cn", "华语片译名应判 cn"
    # 不在 TMDB 表里（查不到）时回退弱判华语
    assert analyze("耳语人 (2026)").region_hint == "cn", "无 TMDB 命中应回退 cn"
    # 中文译名本身不应被改写
    assert analyze("耳语人 (2026)").core == "耳语人"
    print("  OK 耳语人→west / 名侦探柯南→jpkr / 霸王别姬→cn / 查不到→cn")


def test_progress_bare_number_strip():
    """「更新至17」光数字追更写法（不带「集」字）必须从 core 剥净。

    复现「早春晴朗更新至17 → 欧美剧」缺陷：_PROGRESS 原来只认
    「更新至…集/话/話/期」结尾，光数字漏网 → 整串脏词喂给 TMDB 模糊搜索，
    命中错误条目 original_language=en → region=west strong → 国产剧落进欧美剧。

    三层断言：
      1. core/folder 剥干净（命名不再残留「更新至17」）；
      2. TMDB 收到的查询词是干净片名（不是脏串）；
      3. 干净查询词命中 zh → region_hint=cn strong → 分类为国产剧。
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from unittest import mock

    from core import media_meta as mm
    from core.classifier import Classifier
    from core.ident import analyze

    print("\n=== 「更新至17」光数字进度词剥净（早春晴朗缺陷）===")

    # ① 无 TMDB 环境：core/folder 剥干净 + is_pack 保留（剧集包判定看原始标题）
    a = analyze("早春晴朗更新至17")
    assert a.core == "早春晴朗", f"core 应剥成 早春晴朗，实际 {a.core!r}"
    assert a.folder == "早春晴朗", f"folder 不应残留进度词，实际 {a.folder!r}"
    assert a.is_pack is True, "「更新至」原始标题仍应判剧集包（is_pack）"

    # ②③ mock TMDB：查询词必须干净；命中 zh → cn strong → 国产剧
    queries = []
    def fake_lookup(q, year=0):
        queries.append((q, year))
        return {"cn_name": "早春晴朗", "original_language": "zh", "year": 2026}
    with mock.patch.object(mm, "_tmdb_key", lambda: "fake-key"), \
         mock.patch.object(mm, "_tmdb_lookup", fake_lookup):
        a = analyze("早春晴朗更新至17")
        assert queries[-1][0] == "早春晴朗", (
            f"TMDB 查询词应为干净片名 早春晴朗，实际 {queries[-1][0]!r}（脏词会命中错误条目）")
        assert a.region_hint == "cn" and a.region_hint_strong, (
            f"应取 TMDB zh → cn 强提示，实际 {a.region_hint!r} strong={a.region_hint_strong}")
        r = Classifier().classify(a.title, extra=a.folder,
                                  region_hint=a.region_hint,
                                  region_hint_strong=a.region_hint_strong)
        assert r.category == "国产剧", f"国产剧应落 国产剧，实际 {r.category!r}"

    # 回归：带「集」字的旧形态仍整段剥（不能只剥光数字留下尾巴）
    for t in ("早春晴朗更新至17集", "繁花 更新至18集 1080P 国语中字", "狂飙 更新至第15集 1080p"):
        c = analyze(t).core
        assert "更新至" not in c and "17" not in c and "18" not in c and "15" not in c, (
            f"{t!r} 的 core={c!r} 仍残留进度词")
        assert "更新" not in analyze(t).folder, f"{t!r} folder={analyze(t).folder!r} 残留进度词"
    print("  OK core/folder 剥净、TMDB 查询词干净、zh→国产剧；旧带集字形态不受影响")


if __name__ == "__main__":
    test_phrase_region_cn()
    test_phrase_region_jpkr()
    test_chinese_title_always_cn()
    test_chinese_title_tmdb_region()
    test_progress_bare_number_strip()
    print("\n==== 短语地区识别：全部通过 ✅")
