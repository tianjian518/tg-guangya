"""把「完整频道清单」合并进「正在用的配置」，且不丢失光鸭令牌。

为什么需要它？
    容器首次启动时，数据目录里若没有 config.yaml，程序会自动用
    config.example.yaml（只有 ysh365 / seedhub_cc 两个种子频道）复制出一份。
    你精心配的那份多频道 config.yaml 在仓库目录里，而 Docker 只挂载了 ./data，
    导致容器实际只读到了「2 个频道」那份配置。

本工具做的事：
    1. 读取 LIVE 配置（容器真正用的，里面有令牌、转存目录等）-- 保留全部非频道设置
    2. 从 SOURCE 配置（仓库里那份多频道 config.yaml）取出完整频道清单
    3. 合并去重（保留 LIVE 已有的 + 补齐 SOURCE 里的），写回 LIVE
    令牌、output、organize、dedup 等一律不动。

用法（在 FNOS 宿主、仓库目录里、且 ./data 已存在时）：
    docker compose down
    python3 tools/fix_channels.py                 # 默认: live=./data/config.yaml, source=./config.yaml
    docker compose up -d

或显式指定：
    python3 tools/fix_channels.py --live ./data/config.yaml --source ./config.yaml
    python3 tools/fix_channels.py --dry            # 只打印将要合并成多少频道，不写文件
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent  # tg-guangya/


def _norm(name: str) -> str:
    return str(name).strip().lstrip("@").strip().lower()


def merge_channels(live_path: Path, source_path: Path, dry_run: bool = False) -> int:
    if not live_path.exists():
        print(f"❌ LIVE 配置不存在: {live_path}", file=sys.stderr)
        print("   容器启动后才会生成 ./data/config.yaml；请先 docker compose up 一次再运行本工具。",
              file=sys.stderr)
        return -1
    if not source_path.exists():
        print(f"❌ SOURCE 配置不存在: {source_path}", file=sys.stderr)
        return -1

    live = yaml.safe_load(live_path.read_text(encoding="utf-8")) or {}
    src = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}

    live_channels = [str(c).strip().lstrip("@") for c in (live.get("sources", {}).get("channels") or []) if c]
    src_channels = [str(c).strip().lstrip("@") for c in (src.get("sources", {}).get("channels") or []) if c]

    seen = {_norm(c) for c in live_channels}
    added = []
    for c in src_channels:
        if _norm(c) not in seen:
            seen.add(_norm(c))
            added.append(c)
    merged = live_channels + added

    print(f"LIVE  当前频道数: {len(live_channels)}")
    print(f"SOURCE 频道数:    {len(src_channels)}")
    print(f"本次新增:         {len(added)}")
    print(f"合并后频道数:     {len(merged)}")

    if dry_run:
        print("\n[dry-run] 不写文件。合并后清单:")
        for i, c in enumerate(merged, 1):
            print(f"  {i:2d}. {c}")
        return len(merged)

    if not added:
        print("\n✅ 无需改动（LIVE 已包含所有 SOURCE 频道）。")
        return len(merged)

    backup = live_path.with_suffix(live_path.suffix + f".bak-ch{len(merged)}")
    shutil.copyfile(live_path, backup)
    print(f"\n已备份原配置: {backup}")

    live.setdefault("sources", {})
    live["sources"]["channels"] = merged
    live_path.write_text(yaml.safe_dump(live, allow_unicode=True),
                         encoding="utf-8")
    print(f"✅ 已写回 {live_path}（令牌等其它设置保持不变）")
    return len(merged)


def main() -> None:
    ap = argparse.ArgumentParser(description="合并频道清单到运行配置（不丢令牌）")
    ap.add_argument("--live", default=str(REPO / "data" / "config.yaml"),
                    help="容器实际使用的配置（含令牌），默认 ./data/config.yaml")
    ap.add_argument("--source", default=str(REPO / "config.yaml"),
                    help="含完整频道清单的配置，默认仓库 ./config.yaml")
    ap.add_argument("--dry", action="store_true", help="只打印合并结果，不写文件")
    args = ap.parse_args()
    merge_channels(Path(args.live), Path(args.source), dry_run=args.dry)


if __name__ == "__main__":
    main()
