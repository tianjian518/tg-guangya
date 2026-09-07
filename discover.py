"""一次性频道发现工具（不启动常驻服务）。

用法：
    python discover.py --config config.yaml            # 扫描种子源，打印发现的新频道
    python discover.py --config config.yaml --save     # 发现后直接追加进配置
    python discover.py --text "关注 @abc @xyz t.me/foo" # 从一段文本里抠频道

用于验证/手动补充频道，也能在 main.py 的自动发现之外单独跑。
"""
from __future__ import annotations

import argparse
import logging

from core.config import AppConfig
from core.discovery import ChannelDiscovery

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("discover")


def main() -> None:
    ap = argparse.ArgumentParser(description="TG 影视频道发现工具")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--save", action="store_true", help="把发现的新频道写回配置")
    ap.add_argument("--text", default="", help="从给定文本里提取频道（测试用）")
    args = ap.parse_args()

    if args.text:
        names = ChannelDiscovery.extract_names(args.text)
        print("提取到频道:", sorted(names))
        return

    cfg = AppConfig.load(args.config)
    disc = ChannelDiscovery(
        seed_urls=cfg.discovery.seed_urls,
        seed_file=cfg.discovery.seed_file,
        interval_hours=cfg.discovery.interval_hours,
    )
    disc.load_known(cfg.source.channels)
    new = disc.discover_once()
    if not new:
        print("本轮没有发现新频道（也可能是种子源暂不可达，检查网络/seed_urls）。")
        return
    print(f"发现 {len(new)} 个新频道：")
    for n in sorted(new):
        print(f"  @{n}")
    if args.save:
        added = cfg.add_channels(sorted(new), args.config)
        print(f"已写入 {added} 个到 {args.config}")


if __name__ == "__main__":
    main()
