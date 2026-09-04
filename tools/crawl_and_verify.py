#!/usr/bin/env python3
"""从 Telegram 频道目录站挖掘「影视磁力」公开频道，并用 t.me/s 网页实测磁力产出。

跑在 GitHub Actions runner（网络不受限）上。本机网络到不了 t.me 与多数目录站。

流程：
1. 抓 list.tg 搜索页（磁力 / 4K / 电影资源 / 蓝光 / BT 等词），解析出频道用户名；
2. 合并 seeds/candidates_verify.txt 与内置补充候选；
3. 逐个抓 t.me/s/<频道>，统计真实 magnet 链接数并输出报告。
"""
from __future__ import annotations

import json
import re
import sys
import time

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def fetch(url: str, timeout: int = 15) -> str:
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.text
            print(f"  ! {url} -> HTTP {r.status_code}", flush=True)
            return ""
        except Exception as e:
            print(f"  ! {url} 重试{i+1}: {e}", flush=True)
            time.sleep(2)
    return ""


def crawl_listtg(words: list[str]) -> set[str]:
    """抓 list.tg 搜索页，提取频道 @用户名 / t.me 链接 / slug。"""
    found: set[str] = set()
    for w in words:
        import urllib.parse
        url = "https://list.tg/zh/search?q=" + urllib.parse.quote(w)
        html = fetch(url)
        if not html:
            continue
        # 频道卡片里的 @username
        for m in re.finditer(r'@([A-Za-z0-9_]{4,40})', html):
            n = m.group(1)
            if n.lower() not in ("telegram", "list"):
                found.add(n)
        # 形如 /zh/channel/<slug> 或 /c/<slug> 的详情链接 slug 也是频道名
        for m in re.finditer(r'href="[^"]*/(?:channel|c|ch)/?([A-Za-z0-9_]{4,60})', html):
            found.add(m.group(1))
        # t.me/xxx
        for m in re.finditer(r't\.me/([A-Za-z0-9_]{4,40})', html):
            n = m.group(1)
            if n.lower() not in ("telegram", "list"):
                found.add(n)
        print(f"  list.tg 搜「{w}」: +{len(found)} (累计 {len(found)})", flush=True)
        time.sleep(1)
    return found


def crawl_tgstat(words: list[str]) -> set[str]:
    found: set[str] = set()
    for w in words:
        url = f"https://tgstat.com/en/channels/search?q={w}"
        html = fetch(url)
        if not html:
            continue
        for m in re.finditer(r't\.me/([A-Za-z0-9_]{4,40})', html):
            found.add(m.group(1))
        for m in re.finditer(r'@([A-Za-z0-9_]{4,40})', html):
            n = m.group(1)
            if n.lower() not in ("telegram", "tgstat"):
                found.add(n)
        time.sleep(1)
    print(f"  tgstat 共 {len(found)} 个", flush=True)
    return found


def clean(names: set[str]) -> list[str]:
    bad = ("joinchat", "proxy", "socks", "share", "bot", "channel", "telegram",
           "username", "user", "list", "group", "chat", "login", "premium",
           "voicechat", "invoice", "boost", "gift", "addtheme", "addstickers",
           "addlist", "setlanguage", "confirmphone", "bg", "addemoji")
    out = []
    for n in names:
        nl = n.lower()
        if len(n) < 4 or len(n) > 40:
            continue
        if any(b in nl for b in bad):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]+", n):
            continue
        out.append(n)
    return sorted(set(out))


def verify_one(channel: str) -> dict:
    mags: list[str] = []
    msgs = 0
    try:
        html = fetch(f"https://t.me/s/{channel}", timeout=20)
        if not html:
            return {"channel": channel, "error": "无内容(频道不存在/私密/限流)", "msgs": 0,
                    "magnets": 0, "samples": []}
        # t.me/s/<channel> 网页：每个 tgme_widget_message 是最近一条消息
        blocks = re.split(r'tgme_widget_message(?:_wrap)?', html)
        for blk in blocks[1:]:
            msgs += 1
            for m in re.finditer(r'magnet:\?xt=[^"\'<\s]+', blk):
                if m.group(0) not in mags:
                    mags.append(m.group(0))
    except Exception as e:
        return {"channel": channel, "error": str(e)[:60], "msgs": msgs, "magnets": len(mags),
                "samples": mags[:2]}
    return {"channel": channel, "error": "", "msgs": msgs, "magnets": len(mags),
            "samples": mags[:2]}


def main() -> None:
    names: set[str] = set()
    # 1) 本地候选文件
    try:
        for line in open("seeds/candidates_verify.txt", encoding="utf-8"):
            s = line.strip()
            if s and not s.startswith("#"):
                names.add(s.split()[0].lstrip("@"))
    except FileNotFoundError:
        pass
    # 2) 目录站挖掘
    print("== 挖掘 list.tg ==", flush=True)
    names |= crawl_listtg(["磁力", "4K蓝光", "蓝光原盘", "电影资源", "影视磁力", "BT磁力", "remux", "bluray"])
    print("== 挖掘 tgstat ==", flush=True)
    names |= crawl_tgstat(["movie+magnet", "4k+magnet", "movie+torrent"])
    # 3) 补充常见命名（猜测，测到不可达就忽略）
    names |= {
        "Oscar4k", "oscar4k", "oscar4kbluray", "Oscar4KMovies", "4kbluray",
        "remux4k", "4kRemux", "bluray4k", "magnet4k", "4kmagnet",
        "4kfilm", "film4k", "movie4k", "4kmovie",
    }
    cand = clean(names)
    print(f"== 共 {len(cand)} 个候选，开始实测 t.me/s ==", flush=True)
    keep = []
    for ch in cand:
        r = verify_one(ch)
        mark = "✅" if r["magnets"] >= 1 else ("⚠️" if r["msgs"] else "—")
        print(f"{mark} {ch:<24} 消息{r['msgs']:>4} 磁力{r['magnets']:>3} {r.get('error','')}", flush=True)
        if r["magnets"] >= 1:
            keep.append(ch)
            print(f"      ↳ {r['samples'][0][:90]}", flush=True)
        time.sleep(0.5)
    print("\n===== 实测有磁力的频道 =====", flush=True)
    for k in keep:
        print("  " + k, flush=True)
    with open("/tmp/keep_channels.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(keep) if keep else "（无）")


if __name__ == "__main__":
    main()
