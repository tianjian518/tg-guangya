"""真实账号端到端验证：一部电视剧一个文件夹（剧集单集收纳）。

验证目标（用户需求）：
  同一部剧的多集直链，落盘后必须收进 分类目录/剧名文件夹/，
  而不是在分类目录下平铺混在一起。两集必须进【同一个】文件夹。

流程 = 完整复刻线上链路（真实装配 make_handler + 真实 GuangyaClient）：
  频道消息 → KeywordFilter → CloudDedup → Classifier/CategoryResolver
  → create_offline_task → 等完成 → rename 中文名 → move 进剧名文件夹
  （若提交阶段超时，则复刻监控线程补改名路径）

运行：
    python3 tools/e2e_show_folder.py           # 结束后清理测试目录
    python3 tools/e2e_show_folder.py --keep    # 保留现场（排障用）

产物：直链小文件（zip），仅验证命名/收纳逻辑，不占多少空间。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import main  # noqa: E402
from core.classifier import Classifier  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.data_dir import resolve_config_path  # noqa: E402
from core.dedup import CloudDedup  # noqa: E402
from core.guangya import GuangyaClient, GuangyaError  # noqa: E402
from core.ident import analyze, show_folder  # noqa: E402
from core.matcher import KeywordFilter  # noqa: E402
from core.notifier import Notifier  # noqa: E402
from core.organizer import CategoryResolver  # noqa: E402
from core.store import Store  # noqa: E402

TEST_ROOT_NAME = "zz剧集收纳测试"

# 稳定的国内 CDN 直链小文件（光鸦服务端秒完成；境外源实测下载极慢不可用）
EPISODES = [
    ("夏季.2026.S01E04.第4集 2160p",
     "https://www.w3school.com.cn/example/html5/mov_bbb.mp4"),
    ("夏季.2026.S01E05.第5集 2160p",
     "https://www.runoob.com/try/demo_source/movie.mp4"),
]

CATEGORY = "国产剧"
SHOW_DIR = "夏季.2026"
EXPECT_FILES = {"夏季.S01E04.mp4", "夏季.S01E05.mp4"}


class Msg:
    def __init__(self, text, links, channel="e2e", message_id="m1"):
        self.text, self.links, self.channel, self.message_id = text, links, channel, message_id


def build_client(cfg) -> GuangyaClient:
    client = GuangyaClient(
        access_token=cfg.guangya.access_token,
        refresh_token=cfg.guangya.refresh_token,
        client_id=cfg.guangya.client_id,
        device_id=cfg.guangya.device_id,
    )
    client.ensure_token()
    me = client.me() or {}
    who = me.get("nickname") or me.get("phone") or "?"
    print(f"① 已登录真实账号: {who}")
    return client


def process_with_wait(handler, client, store, title: str, url: str, msg_id: str) -> None:
    """handler 处理一条消息；若提交阶段超时，复刻监控线程补改名。"""
    handler(Msg(f"{title} {url}", [url], message_id=msg_id))
    rec = [r for r in store.history(limit=10) if r.hash == main.link_key(url)][0]
    print(f"   提交结果: status={rec.status} task={rec.task_id} cn={rec.cn_folder}")

    if rec.status == "submitted":
        # 任务后台下载中 → 轮询等待完成（最多 ~5 分钟），再复刻监控线程补改名
        print("   任务仍在下载，轮询等待完成 ...")
        deadline = time.time() + 300
        task = None
        while time.time() < deadline:
            time.sleep(8)
            try:
                task = next((t for t in client.list_tasks() if t.task_id == rec.task_id), None)
            except GuangyaError as exc:
                print(f"   查询任务异常（继续等）: {exc}")
                task = None
            if task and task.status == GuangyaClient.STATUS_SUCCESS:
                break
        if not task or task.status != GuangyaClient.STATUS_SUCCESS:
            raise SystemExit(f"   ❌ 任务 {rec.task_id} 5 分钟未完成，直链可能失效")
        store.update(rec.hash, status="done")
        show_dir = ""
        try:
            cand = show_folder(rec.title or "")
            info = analyze(rec.title or "")
            if info.sig and cand and cand != rec.cn_folder:
                show_dir = cand
        except Exception:  # noqa: BLE001
            show_dir = ""
        ok = main._rename_artifact_to_cn(client, rec.task_id, task.name or "",
                                         rec.parent_id, rec.cn_folder, show_dir=show_dir)
        store.update(rec.hash, renamed=1 if ok else 2)
        print(f"   监控路径补改名: {'✅' if ok else '❌'}")


def list_names(client, parent_id: str) -> list[str]:
    return [e.get("name") or "" for e in client.list_dir(parent_id)]


def main_e2e(keep: bool) -> int:
    cfg = AppConfig.load(str(resolve_config_path()))
    client = build_client(cfg)

    base_root = (cfg.output.parent_id or cfg.output.save_path or "").strip()
    print(f"② 用户落盘根: {base_root or '（账号根目录）'}")

    # 测试根目录（复用或新建）
    test_root = ""
    for e in client.list_dir(base_root):
        if (e.get("name") or "").strip() == TEST_ROOT_NAME and e.get("res_type") == 2:
            test_root = e["file_id"]
            break
    if not test_root:
        test_root = client.create_folder(base_root, TEST_ROOT_NAME)
    print(f"③ 测试根目录: {TEST_ROOT_NAME} ({test_root})")

    # 真实装配（对齐线上 main()：分类器/去重参数全部来自用户配置）
    store = Store(":memory:")
    classifier = Classifier(
        mapping=cfg.organize.mapping or None,
        structure=cfg.organize.structure,
        unknown_dir=cfg.organize.unknown_dir,
    )
    resolver = CategoryResolver(client, root_id=test_root,
                                create_missing=cfg.organize.create_missing)
    dedup = CloudDedup(client, resolver, classifier,
                       cloud_check_new=cfg.dedup.cloud_check_new,
                       cache_ttl=0,   # 测试中目录变化快，禁用列表缓存
                       organize_enabled=cfg.organize.enabled,
                       upgrade=cfg.dedup.upgrade,
                       require_cn=cfg.dedup.require_cn)
    handler = main.make_handler(store, client, KeywordFilter([], [], ""),
                                Notifier(console=False), "", 2,
                                classifier=classifier, resolver=resolver,
                                dedup=dedup, organize_enabled=cfg.organize.enabled)

    cat_id, _ = resolver.resolve(CATEGORY)
    print(f"④ 分类目录 {CATEGORY}: {cat_id}")

    for idx, (title, url) in enumerate(EPISODES, 1):
        print(f"\n⑤-{idx} 提交单集: {title}")
        process_with_wait(handler, client, store, title, url, f"m{idx}")
        time.sleep(1)

    # ---------- 终态校验 ----------
    print("\n⑥ 校验云盘终态 ...")
    ok = True
    cat_names = list_names(client, cat_id)
    print(f"   {CATEGORY}/ → {cat_names}")
    flat_files = [n for n in cat_names if n.endswith((".zip", ".mkv", ".mp4"))]
    if flat_files:
        ok = False
        print(f"   ❌ 分类目录下有平铺文件（未收进剧名文件夹）: {flat_files}")

    show_id = ""
    for e in client.list_dir(cat_id):
        if (e.get("name") or "").strip() == SHOW_DIR and e.get("res_type") == 2:
            show_id = e["file_id"]
            break
    if not show_id:
        ok = False
        print(f"   ❌ 未找到剧名文件夹 {SHOW_DIR}")
    else:
        inner = sorted(list_names(client, show_id))
        print(f"   {CATEGORY}/{SHOW_DIR}/ → {inner}")
        expect = EXPECT_FILES
        if not expect.issubset(set(inner)):
            ok = False
            print(f"   ❌ 剧名文件夹内缺少目标文件，期望包含 {expect}")
        else:
            print("   ✅ 两集都在同一个剧名文件夹内")

    recs = store.history(limit=10)
    for r in recs:
        if r.status == "done" and r.renamed != 1:
            ok = False
            print(f"   ❌ 记录 {r.hash[:8]} renamed={r.renamed}（应=1）")

    print("\n⑦ 结论: " + ("✅ 真实账号端到端通过（一部电视剧一个文件夹）" if ok else "❌ 存在问题，见上"))

    if not keep:
        print("\n⑧ 清理测试目录 ...")
        for _ in range(3):
            try:
                client.delete_file(base_root, test_root)
                break
            except GuangyaError as exc:
                print(f"   删除重试: {exc}")
                time.sleep(2)
        try:
            names = [e.get("name") for e in client.list_dir(base_root)]
            print(f"   已清理（{TEST_ROOT_NAME} {'已消失' if TEST_ROOT_NAME not in names else '仍在'}）")
        except GuangyaError:
            pass
    else:
        print("\n⑧ --keep：测试目录保留，可手动查看")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_e2e(keep="--keep" in sys.argv))
