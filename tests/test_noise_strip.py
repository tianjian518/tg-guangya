"""噪声剥离回归测试。

覆盖本次修复的几处机械性缺陷：
  1. 发布组/版本标记（HiveWeb / FRENCH / 60FPS / H.265 / 4kTRASH / FGT …）被剥掉，
     之前因只按空格切分、点号粘连的 token（.60FPS.H.265.）漏掉，core 变成
     "ColdWar60FPS" 之类导致查不到译名。
  2. 标题里所有年份都要剥掉：Cold.War.1994.2026 不能把 2026 粘进 core
     （之前只剥第一个年份，留下 "ColdWar2026"）。
  3. 语种/压制组粘在片名尾部（OppenheimerEnglish / OceansThirteenNF）时，
     按驼峰边界砍尾再查字典能命中真实片名；且与「把 English 塞进噪声白名单」相反，
     《The English Patient》这种片名本身含 English 的不会被破坏。

直接跑：python3 tests/test_noise_strip.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ident import _strip_noise, analyze


def test_release_group_stripped():
    print("\n=== 发布组 / 版本标记剥离 ===")
    cases = {
        # 原始标题（含点号粘连的组名/版本）→ 干净 core
        "Cold.War.1994.2026.1080p.HiveWeb": "ColdWar",
        "Novembre.2022.FRENCH.60FPS.H.265": "Novembre",
        "Oceans.Thirteen.2007.4kTRASH": "OceansThirteen",
        "Searching.2018.1080p.FGT": "Searching",
        "Titanic.1997.BONE.mkv": "Titanic",
    }
    for raw, core in cases.items():
        got = _strip_noise(raw)
        assert got == core, f"_strip_noise({raw!r}) = {got!r}，期望 {core!r}"
        print(f"  OK {raw:<40} → {got!r}")


def test_all_years_stripped():
    print("\n=== 多年份全部剥离（不留粘连）===")
    # 完整链路里，任何年份都不能残留在 core 中。
    # 用字典查不到的片名，避免翻译把 core 变成中文而掩盖年份粘连问题。
    cases = {
        "Some.Movie.Name.1998.2024.Remastered": "SomeMovieName",
        "Show.2010.2021.Complete": "Show",
        "Indie.Film.2005.2099.1080p": "IndieFilm",
    }
    for raw, core in cases.items():
        got = analyze(raw).core
        assert got == core, f"analyze({raw!r}).core = {got!r}，期望 {core!r}（年份粘连未清）"
        print(f"  OK {raw:<40} → core={got!r}")


def test_camel_fallback():
    print("\n=== 驼峰尾部语种/组名兜底查字典 ===")
    cases = {
        "OppenheimerEnglish": "奥本海默",
        "OceansThirteenNF": "十三罗汉",
        "Searching2018FGT": "网络谜踪",
    }
    for raw, cn in cases.items():
        got = analyze(raw).folder
        assert got.startswith(cn), f"analyze({raw!r}).folder = {got!r}，期望以 {cn!r} 开头"
        print(f"  OK {raw:<26} → {got!r}")


def test_english_patient_not_damaged():
    print("\n=== 片名本身含 English 不被误伤 ===")
    # 《The English Patient》（英伦病人）片名里就有 English，
    # camel 兜底绝不能把它当噪声剥掉导致翻译错乱。这里只要保留原片名（不被破坏）即可。
    got = analyze("The English Patient 1996 1080p").folder
    assert "EnglishPatient" in got, f"《The English Patient》被误伤：folder={got!r}"
    print(f"  OK The English Patient → {got!r}（未被破坏）")


def test_resolution_codec_glued_no_boundary():
    print("\n=== 分辨率与编码无分隔粘连（词边界失效场景）===")
    # 复现「耳语人 (2026) 2160pH.26515.78 GB」：2160p 后直接跟 H.265，
    # H.265 后直接跟体积数字，因 \b 在两字母/数字间不存在而整段漏剥。
    # 中文标题做精确断言；英文标题只验证技术 token 被剥离（译名随字典变化，不硬编码）。
    exact = {
        "耳语人 (2026) 2160pH.26515.78 GB": "耳语人.2026",
        "英雄 2026 2160pH265 12.3GB": "英雄.2026",
    }
    for raw, folder in exact.items():
        got = analyze(raw).folder
        assert got == folder, f"analyze({raw!r}).folder = {got!r}，期望 {folder!r}"
        print(f"  OK {raw:<40} → {got!r}")
    tech = [
        "Cold.War.1994.2026.2160p.60FPS.H.265",
        "RandomTitleXYZ.2023.2160p.HDR.WEB-DL.x265.10bit",
    ]
    for raw in tech:
        f = analyze(raw).folder
        banned = ("2160p", "60FPS", "H.265", "x265", "HDR", "10bit", "WEB-DL")
        leaked = [b for b in banned if b in f]
        assert not leaked, f"analyze({raw!r}).folder = {f!r} 残留技术词 {leaked}"
        print(f"  OK {raw:<40} → 技术词已剥离（{f!r}）")


if __name__ == "__main__":
    test_release_group_stripped()
    test_all_years_stripped()
    test_camel_fallback()
    test_english_patient_not_damaged()
    test_resolution_codec_glued_no_boundary()
    print("\n==== 噪声剥离：全部通过 ✅")
