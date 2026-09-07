"""把网盘里「历史落盘的英文命名产物」批量改成中文名。

背景
----
新链路（v1.4+）落盘的产物都会自动改中文名；但历史版本落下的产物
（如 tg转存/欧美电影/Searching.2018.1080p.BluRay.REMUX...FGT）还躺在盘里。
本工具扫描分类目录，对每个英文命名的产物算中文名并 rename：

  - 文件夹 → 改成中文名（如 网络谜踪.2018）
  - 单文件 → 保留原扩展名（如 流浪地球2.2023.mkv）
  - 已是中文名 / 算不出译名的 → 跳过（绝不盲改）

安全机制
--------
1. 默认 **dry-run**：只打印「将改成什么」，不动云端任何东西。
2. 确认无误后加 --apply 才真正改名。
3. 每改一个就重新列举校验，失败会在结尾汇总。

用法
----
    python3 tools/fix_legacy_names.py --root <落盘目录名或fileId>   # 预览
    python3 tools/fix_legacy_names.py --root tg转存 --apply         # 执行
    python3 tools/fix_legacy_names.py                               # 自动探测根目录
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.config import AppConfig  # noqa: E402
from core.data_dir import resolve_config_path  # noqa: E402
from core.guangya import GuangyaClient, GuangyaError  # noqa: E402
from core.ident import analyze  # noqa: E402

# 常见分类目录名（用于自动探测落盘根目录）
_KNOWN_CATS = {"华语电影", "欧美电影", "日韩电影", "其他电影", "国产剧", "欧美剧",
               "日韩剧", "其他剧集", "国产动漫", "日本动漫", "欧美动漫", "纪录片",
               "综艺", "演唱会", "未分类"}

_HAS_CN = re.compile(r"[一-鿿]")


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    base = s.rsplit(".", 1)[0] if "." in s else s
    return re.sub(r"[^0-9a-z一-鿿]", "", base)


def detect_root(client: GuangyaClient) -> tuple[str, str]:
    """自动探测落盘根目录：找含 ≥2 个已知分类名的目录。返回 (fid, 路径描述)。"""
    for e in client.list_dir(""):
        if e.get("res_type") != 2:
            continue
        try:
            kids = client.list_dir(e["file_id"])
        except GuangyaError:
            continue
        hits = sum(1 for k in kids if k.get("res_type") == 2 and k.get("name") in _KNOWN_CATS)
        if hits >= 2:
            return e["file_id"], e.get("name") or ""
    return "", ""


def plan_rename(entry: dict) -> tuple[str, str] | None:
    """算出一个产物的新中文名。返回 (旧名, 新名)；None 表示跳过。"""
    name = entry.get("name") or ""
    if _HAS_CN.search(name):
        return None                                   # 已含中文，视为已命名
    is_file = entry.get("res_type") == 1
    ext = name.rsplit(".", 1)[1] if is_file and "." in name else ""
    try:
        info = analyze(name)
    except Exception:
        return None
    folder = info.folder or ""
    if not folder or not _HAS_CN.search(folder) or _norm(folder) == _norm(name):
        return None                                   # 算不出中文，或与原名等价
    return name, f"{folder}.{ext}" if ext else folder


def main() -> int:
    ap = argparse.ArgumentParser(description="历史英文产物批量改中文名（默认 dry-run）")
    ap.add_argument("--root", default="", help="落盘根目录（名称或 fileId），留空自动探测")
    ap.add_argument("--apply", action="store_true", help="真正执行改名（默认只预览）")
    ap.add_argument("--limit", type=int, default=0, help="每个目录最多处理 N 条（0=不限）")
    args = ap.parse_args()

    cfg = AppConfig.load(str(resolve_config_path()))
    client = GuangyaClient(access_token=cfg.guangya.access_token,
                           refresh_token=cfg.guangya.refresh_token,
                           client_id=cfg.guangya.client_id,
                           device_id=cfg.guangya.device_id)
    client.ensure_token()
    me = client.me() or {}
    print(f"✓ 已登录: {me.get('name') or me.get('nickname') or '?'}")

    root, root_desc = "", ""
    if args.root:
        if args.root.isdigit():
            root = args.root
            root_desc = next((e.get("name") for e in client.list_dir("")
                              if e.get("file_id") == root) or args.root)
        else:
            hit = next((e for e in client.list_dir("")
                        if e.get("name") == args.root and e.get("res_type") == 2), None)
            if not hit:
                print(f"❌ 根目录下找不到名为「{args.root}」的目录")
                return 1
            root, root_desc = hit["file_id"], hit["name"]
    else:
        root, root_desc = detect_root(client)
        if not root:
            print("❌ 自动探测失败：根目录下没有含 ≥2 个分类目录的文件夹。请用 --root 指定。")
            return 1
    print(f"✓ 落盘根目录: {root_desc} ({root})  模式: {'APPLY 实改' if args.apply else 'DRY-RUN 预览'}")

    done = failed = skipped = 0
    for cat in client.list_dir(root):
        if cat.get("res_type") != 2:
            continue
        cat_name = cat.get("name") or ""
        entries = client.list_dir(cat["file_id"])
        if args.limit:
            entries = entries[:args.limit]
        n_this = 0
        for entry in entries:
            plan = plan_rename(entry)
            if plan is None:
                skipped += 1
                continue
            old, new = plan
            if not args.apply:
                print(f"  [预览] {cat_name}/{old}\n         → {new}")
                done += 1
                n_this += 1
                continue
            try:
                client.rename_file(entry["file_id"], new)
                now = [e.get("name") for e in client.list_dir(cat["file_id"])]
                if new in now and old not in now:
                    print(f"  ✅ {cat_name}/{old} → {new}")
                    done += 1
                    n_this += 1
                else:
                    print(f"  ⚠️ 改名后校验未通过: {cat_name}/{old} → {new}")
                    failed += 1
            except GuangyaError as exc:
                print(f"  ❌ {cat_name}/{old}: {str(exc)[:100]}")
                failed += 1
        if n_this:
            print(f"  （{cat_name}: {n_this} 条）")

    print(f"\n===== 汇总 =====")
    print(f"可改/已改: {done}  |  跳过（已中文或算不出）: {skipped}  |  失败: {failed}")
    if not args.apply and done:
        print("以上为预览。确认无误后追加 --apply 执行真实改名。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
