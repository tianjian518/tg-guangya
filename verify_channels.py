"""候选频道磁力产出验证工具（无需 Telegram 账号）。

原理：公开频道都有网页版 t.me/s/<频道名>，本工具逐个抓取最近若干页消息，
统计其中包含的磁力（及迅雷/电驴）链接数量，据此判断「这个频道到底发不发磁力」。

用途：
- 给 discovery 种子里那些「疑似发磁力」的候选频道做一次性体检，
  只把真有产出的留下，避免把一堆网盘分享频道（只发夸克/天翼链接）塞进监控。

用法：
    # 验证指定频道
    python verify_channels.py --channels hdhhd21 zhenyingsg xingqiump4

    # 验证种子文件里「候选待验证」区段（本工具会自动识别带注释块的频道）
    python verify_channels.py --seed seeds/channels_seed.txt

    # 只输出值得保留的频道（磁力数 >= 1），可直接粘进 config.yaml 的 sources.channels
    python verify_channels.py --seed seeds/channels_seed.txt --keep

依赖：仅公开网页抓取，不需要 api_id / api_hash，也不需要登录。
"""
from __future__ import annotations

import argparse
import logging
import re
import time

from adapters.web_scraper import WebScraper, extract_links

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger("verify")

# 候选区段标记：种子文件里以该注释开头的块会被当成「待验证频道」
CANDIDATE_MARK = "候选待验证"


def _channels_from_args(args) -> list[str]:
    chs: list[str] = []
    if args.channels:
        chs += [c.strip().lstrip("@") for c in args.channels if c.strip()]
    if args.seed:
        try:
            with open(args.seed, "r", encoding="utf-8") as f:
                in_block = False
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        in_block = CANDIDATE_MARK in line
                        continue
                    if in_block:
                        chs.append(s.lstrip("@").split()[0])
        except FileNotFoundError:
            log.warning("种子文件不存在: %s", args.seed)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for c in chs:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def verify_one(scraper: WebScraper, channel: str, pages: int) -> dict:
    msgs = 0
    magnets = 0
    others = 0
    samples: list[str] = []
    try:
        for m in scraper.iter_history(channel, pages=pages):
            msgs += 1
            links = m.links
            mags = [u for u in links if u.lower().startswith("magnet:")]
            magnets += len(mags)
            others += len(links) - len(mags)
            if mags and len(samples) < 3:
                samples.append(mags[0][:80])
    except Exception as exc:  # 频道不可达 / 不存在
        return {"channel": channel, "error": str(exc), "msgs": 0,
                "magnets": 0, "others": 0, "samples": []}
    return {"channel": channel, "error": "", "msgs": msgs,
            "magnets": magnets, "others": others, "samples": samples}


def main() -> None:
    ap = argparse.ArgumentParser(description="验证候选频道是否真发磁力")
    ap.add_argument("--channels", nargs="+", help="直接指定频道用户名列表")
    ap.add_argument("--seed", default="seeds/channels_seed.txt", help="待验证种子区段所在文件")
    ap.add_argument("--pages", type=int, default=3, help="每个频道回溯的页数（默认 3）")
    ap.add_argument("--min-magnets", type=int, default=1, help="判定为「值得保留」的最低磁力数")
    ap.add_argument("--keep", action="store_true", help="只输出值得保留的频道清单")
    ap.add_argument("--delay", type=float, default=1.0, help="频道之间的间隔秒数（防频率限制）")
    args = ap.parse_args()

    channels = _channels_from_args(args)
    if not channels:
        print("没有可验证的频道。请用 --channels 指定，或确保种子文件含「候选待验证」区段。")
        return

    scraper = WebScraper(channels, interval=max(5, int(args.delay)))
    results = []
    for ch in channels:
        print(f"扫描 @{ch} ...", flush=True)
        r = verify_one(scraper, ch, args.pages)
        results.append(r)
        time.sleep(args.delay)

    # 报告
    print("\n=== 验证结果 ===")
    print(f"{'频道':<20}{'消息':>6}{'磁力':>6}{'其他链':>7}  结论")
    keep: list[str] = []
    for r in results:
        if r.get("error"):
            verdict = f"不可达({r['error'][:30]})"
        elif r["magnets"] >= args.min_magnets:
            verdict = "✅ 值得保留"
            keep.append(r["channel"])
        else:
            verdict = "⚠️ 无磁力产出，建议不放进监控"
        print(f"{r['channel']:<20}{r['msgs']:>6}{r['magnets']:>6}{r['others']:>7}  {verdict}")
        for s in r["samples"]:
            print(f"      ↳ {s}")

    if args.keep:
        print("\n=== 建议保留的频道（可直接粘进 sources.channels）===")
        print("  " + "\n  ".join(keep) if keep else "  （无）")
    else:
        print(f"\n小结：{len(keep)}/{len(results)} 个频道有磁力产出。")


if __name__ == "__main__":
    main()
