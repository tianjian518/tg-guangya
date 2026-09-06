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


if __name__ == "__main__":
    test_phrase_region_cn()
    test_phrase_region_jpkr()
    test_chinese_title_always_cn()
    print("\n==== 短语地区识别：全部通过 ✅")
