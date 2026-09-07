"""截图标题字典覆盖回归测试。

用户的核心诉求：「无论是频道里的链接，还是发给机器人的链接，都要改成中文，然后落到云盘里。」
本测试用用户贴出的截图里那 9 条真实标题，钉死「最终落盘文件夹名 = 中文片名 (年份)」。

直接用 analyze() 跑（与运行时一致），断言 folder 字段就是正确的中文落盘名。
注意：这里只验证「命名」，分类目录归属由 test_phrase_region.py 覆盖。

直接跑：python3 tests/test_dict_titles.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ident import analyze


# (真实噪杂标题, 期望的落盘文件夹名)
SCREENSHOT_CASES = [
    ("Searching.2018.1080p.FGT",                       "网络谜踪 (2018)"),
    ("Oceans.Thirteen.2007.4kTRASH",                   "十三罗汉 (2007)"),
    ("Oceans.Eleven.2001.4kTRASH",                     "十一罗汉 (2001)"),
    ("Novembre.2022.FRENCH.60FPS.H.265",               "十一月 (2022)"),
    ("New Dragon Gate Inn [1992] 1080p BluRay",         "新龙门客栈 (1992)"),
    ("House.On.The.Edge.Of.The.Park.1980.ITALIAN.mkv", "公园边缘的房子 (1980)"),
    ("周星驰大话西游系列 1080p",                          "周星驰大话西游系列"),
    ("Titanic.1997.BONE.mkv",                          "泰坦尼克号 (1997)"),
    ("Cold.War.1994.2026.1080p.HiveWeb.mkv",           "冷战 (1994)"),
    # 旧版直接「拒绝」的纯英文片，现要求落盘（译得出中文就译）
    ("Oppenheimer.2023.4K.WEB-DL.English",             "奥本海默 (2023)"),
]


def test_screenshot_titles_chinese_folder():
    print("\n=== 截图标题 → 中文落盘名 ===")
    bad = []
    for raw, expected in SCREENSHOT_CASES:
        got = analyze(raw).folder
        ok = got == expected
        if not ok:
            bad.append(f"{raw!r} → 实际 {got!r}，期望 {expected!r}")
        print(f"  {'OK ' if ok else 'BAD'} {raw[:44]:<46} → {got!r}")
    assert not bad, "以下标题未落到预期中文名：\n  " + "\n  ".join(bad)


def test_folder_is_chinese():
    print("\n=== 落盘名必须含中文（不许纯英文落盘）===")
    bad = []
    for raw, _ in SCREENSHOT_CASES:
        folder = analyze(raw).folder
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in folder)
        if not has_cjk:
            bad.append(f"{raw!r} → {folder!r}（无中文）")
        print(f"  {'OK ' if has_cjk else 'BAD'} {raw[:44]:<46} → {folder!r}")
    # 唯一例外：字典查不到译名的冷门片才允许英文名落盘（见 dedup 不丢资源）；
    # 上面的截图标题都是常见片，必须都能译出中文。
    assert not bad, "以下标题落盘名不含中文：\n  " + "\n  ".join(bad)


if __name__ == "__main__":
    test_screenshot_titles_chinese_folder()
    test_folder_is_chinese()
    print("\n==== 截图标题中文命名：全部通过 ✅")
