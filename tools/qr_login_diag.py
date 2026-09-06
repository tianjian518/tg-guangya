"""扫码登录 + 改名实测 一键脚本（用于真实账号验证光鸭改名接口）。

不读取项目配置，token 来自扫码登录结果（/tmp/qr_token.json）。
用法：
  python3 tools/qr_login_diag.py login    # 后台运行：轮询等待 App 扫码，成功后保存 token
  python3 tools/qr_login_diag.py rename   # 用 token 实测改名接口（复用 diagnose_rename 的探测逻辑）
"""
from __future__ import annotations

import sys
import json
import time

sys.path.insert(0, ".")

from core.guangya import GuangyaClient, GuangyaError

SESSION_FILE = "/workspace/qr_session.json"
TOKEN_FILE = "/tmp/qr_token.json"
PROBE_PREFIX = "zz重命名探测"
EXPECT_CN = "中文改名探测OK"
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


def phase_login():
    sess = json.load(open(SESSION_FILE))
    c = GuangyaClient(
        access_token="",
        refresh_token="",
        client_id=sess["client_id"],
        device_id=sess["device_id"],
    )
    print("轮询等待扫码确认... device_code=", sess["device_code"], flush=True)
    t0 = time.time()
    status = c.poll_qr_login(sess["device_code"], interval=2, timeout=600)
    print("poll status:", status, "耗时", int(time.time() - t0), "s", flush=True)
    if status == "success":
        json.dump(
            {
                "access_token": c._access,
                "refresh_token": c._refresh,
                "client_id": sess["client_id"],
                "device_id": sess["device_id"],
            },
            open(TOKEN_FILE, "w"),
        )
        print("TOKEN_SAVED " + TOKEN_FILE, flush=True)
    else:
        print("LOGIN_FAILED " + status, flush=True)


def phase_rename():
    tok = json.load(open(TOKEN_FILE))
    c = GuangyaClient(
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token", ""),
        client_id=tok.get("client_id", ""),
        device_id=tok.get("device_id", ""),
    )
    c.ensure_token()
    me = c.me() or {}
    print("① 已登录:", json.dumps(me, ensure_ascii=False)[:300])
    root = ""
    print(f"② 探测根目录: {root or '（根目录）'}")

    print("\n③ 逐个尝试候选 rename 接口（每个都建一个临时目录测试）...")
    results = []
    created = []
    for idx, (path, name_key) in enumerate(CANDIDATES, 1):
        en_name = f"{PROBE_PREFIX}{idx}"
        try:
            fid = c.create_folder(root, en_name)
        except GuangyaError as exc:
            print(f"   [{idx}] {path} ({name_key}) — 建目录失败，跳过: {exc}")
            results.append((path, name_key, "建目录失败"))
            continue
        created.append((fid, en_name))
        time.sleep(0.4)
        api_err = ""
        try:
            c._api_post(path, {"fileId": fid, name_key: EXPECT_CN})
        except GuangyaError as exc:
            api_err = str(exc)[:80]
        time.sleep(0.6)
        still_en = _find_dir(c, root, en_name)
        now_cn = _find_dir(c, root, EXPECT_CN)
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
        for _ in range(2):
            try:
                c.delete_file(root, fid)
            except GuangyaError:
                pass
            time.sleep(0.3)
        gone = _find_dir(c, root, en_name)
        cn = _find_dir(c, root, EXPECT_CN)
        if not gone and not cn:
            print(f"   ✅ 已清理 {en_name}")
        else:
            print(f"   ⚠️ 残留需手动删除: {en_name if gone else EXPECT_CN}")

    print("\n===== 结论 =====")
    working = [r for r in results if "生效" in r[2]]
    if working:
        path, name_key, _ = working[0]
        print(f"✅ 光鸭支持重命名：{path}（文件名键 = {name_key}）")
    else:
        print("❌ 四种候选接口全部无效 —— 光鸭服务端不支持客户端改名。")
    return 0


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "login"
    if phase == "login":
        phase_login()
    elif phase == "rename":
        sys.exit(phase_rename())
    else:
        print("用法: python3 tools/qr_login_diag.py [login|rename]")
