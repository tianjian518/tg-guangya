"""把分类目录下「历史平铺的单集文件」收进剧名文件夹（一部电视剧一个文件夹）。

背景
----
新链路（2026-09 剧集收纳版）落盘的单集会自动收进 分类目录/剧名.年份/；
但历史版本（含 MoviePilot 等外部工具）转存的单集还平铺在分类目录里，
不同剧集混在一起——这正是用户抱怨「国产剧下面都是各个剧集混在一起」。

本工具扫描剧集/动漫类分类目录，对每个【平铺的单集文件】（带集号签名）：

  1. 从文件名解析 剧名/年份/集号（ident.analyze，与新链路同源）
  2. 英文命名的先查译名字典（译不出 → 跳过并报告，绝不盲建英文夹）
  3. 建（或复用）剧名文件夹：剧名.年份（show_folder，与新链路一致）
  4. 单集文件重命名为新链路规范名：剧名.SxxExx.ext（顺带清掉 {tmdbid-xxx} 等尾巴）
  5. move 进剧名文件夹

安全机制
--------
1. 默认 **dry-run**：只打印「将改成什么、收进哪」，不动云端任何东西。
2. 确认无误后加 --apply 才真正执行。
3. 每步（rename/move）都重新列举校验；目标文件夹已有同名文件 → 跳过防覆盖。
4. 只动【带集号的平铺单文件】；整季包/电影文件夹一律不碰。

用法
----
    python3 tools/organize_legacy_episodes.py --root tg转存          # 预览
    python3 tools/organize_legacy_episodes.py --root tg转存 --apply  # 执行
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.config import AppConfig  # noqa: E402
from core.data_dir import resolve_config_path  # noqa: E402
from core.guangya import GuangyaClient, GuangyaError  # noqa: E402
from core.ident import _lookup_en, analyze, show_folder  # noqa: E402
from core.naming import build_cn_filename  # noqa: E402
from tools.fix_legacy_names import _norm, detect_root  # noqa: E402

# 剧集/动漫类分类目录（电影/综艺/演唱会无「按剧归档」概念，不扫）
TV_CATS = {"国产剧", "欧美剧", "日韩剧", "其他剧集",
           "国产动漫", "日本动漫", "欧美动漫"}

_HAS_CN = re.compile(r"[一-鿿]")


def plan_collect(entry: dict) -> tuple[str, str, str] | None:
    """算一个平铺单集的收纳方案。返回 (原名, 新文件名, 剧名文件夹)；None=跳过。

    跳过条件：不是文件 / 无集号签名（整包交给改名工具）/ 译不出中文（英文单集）。
    新文件名与剧名文件夹用与新链路完全同源的 build_cn_filename / show_folder，
    保证「历史文件收进来之后」和新落盘的产物长得一模一样。
    """
    name = entry.get("name") or ""
    if entry.get("res_type") != 1:                 # 只收单集文件，文件夹不碰
        return None
    try:
        info = analyze(name)
    except Exception:
        return None
    if not info.sig or not info.core:              # 无集号 → 不是单集
        return None

    ext = name.rsplit(".", 1)[1] if "." in name else ""
    if _HAS_CN.search(info.core):
        target = build_cn_filename(name, ext)      # 中文标题：与新链路同一函数
        show_dir = show_folder(name)
    else:
        # 英文命名单集：先查译名字典（与新链路同一本），译不出不硬收
        translated, _region = _lookup_en(info.core, info.year)
        if not translated:
            return None
        target = f"{translated}.{info.sig.upper()}.{ext}" if ext else \
            f"{translated}.{info.sig.upper()}"
        show_dir = f"{translated}.{info.year}" if info.year else translated

    if not _HAS_CN.search(show_dir):
        return None
    if _norm(target) == _norm(name):               # 名字与目标等价（仅收进文件夹，不改名）
        target = name
    return name, target, show_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="历史平铺单集收进剧名文件夹（默认 dry-run）")
    ap.add_argument("--root", default="", help="落盘根目录（名称或 fileId），留空自动探测")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    args = ap.parse_args()

    cfg = AppConfig.load(str(resolve_config_path()))
    client = GuangyaClient(access_token=cfg.guangya.access_token,
                           refresh_token=cfg.guangya.refresh_token,
                           client_id=cfg.guangya.client_id,
                           device_id=cfg.guangya.device_id)
    client.ensure_token()
    print("✓ 已登录")

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
                print(f"❌ 根目录下找不到「{args.root}」")
                return 1
            root, root_desc = hit["file_id"], hit["name"]
    else:
        root, root_desc = detect_root(client)
        if not root:
            print("❌ 自动探测失败，请 --root 指定")
            return 1
    print(f"✓ 落盘根目录: {root_desc} ({root})  模式: {'APPLY 实改' if args.apply else 'DRY-RUN 预览'}\n")

    moved = renamed_n = failed = skipped = 0
    for cat in client.list_dir(root):
        if cat.get("res_type") != 2 or (cat.get("name") or "") not in TV_CATS:
            continue
        cat_name = cat.get("name") or ""
        cat_id = cat["file_id"]
        entries = client.list_dir(cat_id)
        for entry in entries:
            plan = plan_collect(entry)
            if plan is None:
                skipped += 1
                continue
            old, target, show_dir = plan
            print(f"[{'执行' if args.apply else '预览'}] {cat_name}/{old}")
            print(f"         → {cat_name}/{show_dir}/{target}")
            if not args.apply:
                moved += 1
                continue

            try:
                # ① 建（或复用）剧名文件夹
                show_id = ""
                for e in client.list_dir(cat_id):
                    if e.get("res_type") == 2 and (e.get("name") or "").strip() == show_dir:
                        show_id = e["file_id"]
                        break
                if not show_id:
                    show_id = client.create_folder(cat_id, show_dir)
                    print(f"         已创建文件夹 {show_dir}")
                # ② 目标文件夹已有同名 → 跳过防重复/覆盖
                inner = [e.get("name") for e in client.list_dir(show_id)]
                if target in inner:
                    print("         ⚠️ 剧名文件夹内已有同名文件，跳过（请手动核对）")
                    skipped += 1
                    continue
                # ③ 先改名（新链路规范名），再收进文件夹
                fid = entry["file_id"]
                if target != old:
                    client.rename_file(fid, target)
                    time.sleep(0.5)
                    now = [e.get("name") for e in client.list_dir(cat_id)]
                    if target not in now:
                        print("         ❌ 改名后校验未通过，放弃收纳")
                        failed += 1
                        continue
                    renamed_n += 1
                # ④ move
                client.move_file(fid, show_id)
                inner = [e.get("name") for e in client.list_dir(show_id)]
                if target in inner:
                    print("         ✅ 已收纳")
                    moved += 1
                else:
                    print("         ⚠️ 移动后校验未通过")
                    failed += 1
            except GuangyaError as exc:
                print(f"         ❌ {str(exc)[:100]}")
                failed += 1
        print()

    print("===== 汇总 =====")
    print(f"收纳: {moved}（其中顺带改名 {renamed_n}）  |  跳过: {skipped}  |  失败: {failed}")
    if not args.apply and moved:
        print("以上为预览。确认无误后追加 --apply 执行真实收纳。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
