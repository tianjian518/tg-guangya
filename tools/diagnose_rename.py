"""诊断光鸭云盘到底支不支持「重命名」——这是「落盘中文命名」的成败关键。

为什么需要这个脚本
-------------------
代码侧已经能把频道标题算成正确的中文名（core/ident.py，可用
`python3 -m pytest tests/ -q` 与 tools/ 下的用例验证）。但**算出来**
和**真能写到云盘上**是两件事，后者取决于光鸭服务端是否支持：

  ① 创建离线任务时用 fileName 指定名字（create_offline_task 的 cn_name）
  ② 任务完成后调 rename 接口改名（_rename_folder_to_cn）

光鸭没有公开 API 文档，这两个能力都是照着 LitePan 逆向代码**推测**的，
从未在真实账号上验证过。若服务端根本不认，那无论本地算出多正确的中文名，
云盘里仍然只会是种子英文名——表现就是「升级了却还是英文」「直接落盘」。

本脚本不下载任何资源，只做轻量探测（建目录 → 改名 → 复查 → 清理），
几秒内出结论。请在**已登录（token 有效）的环境**里运行：

    python3 tools/diagnose_rename.py

它会逐个尝试几种候选接口，明确告诉你哪条路走得通。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.config import AppConfig  # noqa: E402
from core.data_dir import resolve_config_path  # noqa: E402
from core.guangya import GuangyaClient, GuangyaError  # noqa: E402

PROBE_PREFIX = "zz重命名探测"
EXPECT_CN = "中文改名探测OK"

# 候选：(接口路径, 文件名 body 键)。光鸭未公开文档，逐个试出真实可用的那个。
CANDIDATES = [
    ("/userres/v1/file/rename", "fileName"),
    ("/userres/v1/file/rename_file", "fileName"),
    ("/userres/v1/file/rename", "name"),
    ("/userres/v1/rename", "fileName"),
]


def _find_dir(client, parent_id: str, name: str):
    """在目录下按名字精确找文件夹，返回 entry 或 None。"""
    try:
        for e in client.list_dir(parent_id):
            if (e.get("name") or "").strip() == name and e.get("res_type") == 2:
                return e
    except GuangyaError:
        pass
    return None


def main() -> int:
    cfg_path = str(resolve_config_path())
    cfg = AppConfig.load(cfg_path)
    if not (cfg.guangya.access_token or "").strip() and not (cfg.guangya.refresh_token or "").strip():
        print("❌ 配置文件里没有光鸭 token，请先运行 `python login.py` 扫码登录。")
        print(f"   配置文件: {cfg_path}")
        return 1

    client = GuangyaClient(
        access_token=cfg.guangya.access_token,
        refresh_token=cfg.guangya.refresh_token,
        client_id=cfg.guangya.client_id,
        device_id=cfg.guangya.device_id,
    )

    print("① 校验登录状态...")
    try:
        client.ensure_token()
        me = client.me() or {}
        who = (me.get("nickname") or me.get("phone") or me.get("data") or {}).get(
            "nickname") if isinstance(me.get("data"), dict) else None
        print(f"   ✅ 已登录{('，用户: ' + str(who)) if who else ''}")
    except GuangyaError as exc:
        print(f"   ❌ 登录失效或接口不通: {exc}")
        print("   请先 `python login.py` 重新扫码。")
        return 1

    root = (cfg.output.parent_id or cfg.output.save_path or "").strip()
    print(f"② 探测目录: {root or '（根目录）'}")

    print("\n③ 逐个尝试候选 rename 接口（每个都建一个临时目录测试）...")
    results = []
    created = []
    for idx, (path, name_key) in enumerate(CANDIDATES, 1):
        en_name = f"{PROBE_PREFIX}{idx}"
        try:
            fid = client.create_folder(root, en_name)
        except GuangyaError as exc:
            print(f"   [{idx}] {path} ({name_key}) — 建目录失败，跳过: {exc}")
            results.append((path, name_key, "建目录失败"))
            continue
        created.append((fid, en_name))
        time.sleep(0.4)

        # 调接口改名
        api_err = ""
        try:
            client._api_post(path, {"fileId": fid, name_key: EXPECT_CN})
        except GuangyaError as exc:
            api_err = str(exc)[:80]
        time.sleep(0.6)

        # 复查：目录还在不在、名字变没变
        still_en = _find_dir(client, root, en_name)
        now_cn = _find_dir(client, root, EXPECT_CN)
        if now_cn and not still_en:
            verdict = "✅ 生效"
        elif now_cn and still_en:
            verdict = "⚠️ 疑似生效（旧名仍在，可能只是列表缓存）"
        else:
            verdict = f"❌ 无效{'；接口报错: ' + api_err if api_err else '（接口返回成功但名字没变）'}"
        print(f"   [{idx}] {path} (键={name_key}) — {verdict}")
        results.append((path, name_key, verdict))

    print("\n④ 清理临时目录...")
    for fid, en_name in created:
        for _ in range(2):  # 改名成功的话，英文名那条已经不存在了，两种都试
            try:
                client.delete_file(root, fid)
            except GuangyaError:
                pass
            time.sleep(0.3)
        gone = _find_dir(client, root, en_name)
        cn = _find_dir(client, root, EXPECT_CN)
        if not gone and not cn:
            print(f"   ✅ 已清理 {en_name}")
        else:
            print(f"   ⚠️ 残留需手动删除: {en_name if gone else EXPECT_CN}")

    print("\n===== 结论 =====")
    working = [r for r in results if "生效" in r[2]]
    if working:
        path, name_key, _ = working[0]
        print(f"✅ 光鸭支持重命名：{path}（文件名键 = {name_key}）")
        print("   → 把 core/guangya.py 的 rename_file() 改成这个路径即可，")
        print("     落盘后改名会自动生效，中文命名这条路走得通。")
    else:
        print("❌ 四种候选接口全部无效 —— 光鸭服务端不支持重命名。")
        print("   → 这意味着「离线下载后改名」这条路在架构上走不通：")
        print("     离线下载的文件名由服务端按种子内容生成，客户端改不了。")
        print("   → 可选替代方案：")
        print("      1) 只保证「分类目录是中文」（已实现），文件本身保留英文原名；")
        print("      2) 改用支持自定义文件名的网盘（如 115 / 阿里云盘 OpenAPI）；")
        print("      3) 自建 Aria2 下载到本地，重命名后再上传。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
