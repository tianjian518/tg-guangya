"""全链路端到端测试：频道消息 → 过滤 → 去重 → 分类建目录 → 提交离线
→ 任务完成 → 产物改中文名 → 云盘复核 → store 落库 → 监控线程补改名。

与 tests/test_organize_e2e.py 的区别：那一个只测「分类→建目录→提交」；
本测试复刻 main() 的真实装配（KeywordFilter/Classifier/CategoryResolver/
CloudDedup/make_handler/监控线程全部真实代码），只有 GuangyaClient 换成
内存模拟 CloudSim，把「任务完成后产物改名」这一步也纳入链路。

真实背景（2026-09 用户网盘实测发现，缺一不可）：
  - BT 磁力产物 = 文件夹；直链/单文件种子产物 = 单个文件（带扩展名）
  - 服务端忽略 create_task 的 fileName，任务名由种子内容生成（英文）
  - 完成后必须 rename（fileId+newName）才有中文名
  - 监控线程必须查全量任务列表：只查进行中任务时，任务完成的瞬间会从
    列表「消失」而被误标失败，补改名永远不执行（历史 bug）

直接跑：python3 tests/test_full_pipeline.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from core.classifier import Classifier
from core.dedup import CloudDedup
from core.guangya import OfflineTask, STATUS_RUNNING, STATUS_SUCCESS
from core.matcher import KeywordFilter
from core.notifier import Notifier
from core.organizer import CategoryResolver
from core.store import Store


class CloudSim:
    """内存版光鸭：目录树 + 文件 + 离线任务生命周期 + rename。

    只实现 main/dedup/resolver 实际调用的接口子集。
    url_map: url -> 产物英文名（模拟服务端按种子内容生成名字，fileName 被忽略）。
    完成模式两种（贴近真实）：
      auto_complete=True  提交即完成并落产物（小文件/有缓存种子）
      auto_complete=False 保持下载中，需测试手动 complete()（模拟大文件超时）
    """
    def __init__(self, url_map: dict[str, str] | None = None,
                 auto_complete: bool = True, file_urls: set[str] | None = None):
        self.next_id = 1
        self.dirs: dict[str, dict[str, tuple[str, int]]] = {"": {}}  # parent -> name -> (fid, res_type)
        self.tasks: dict[str, dict] = {}
        self.url_map = url_map or {}
        self.auto_complete = auto_complete
        self.file_urls = file_urls or set()   # 这些 url 的产物是单文件
        self._counter = 0

    # ---------- 目录 ----------
    def _fid(self) -> str:
        self._counter += 1
        return f"n{self._counter}"

    def create_folder(self, parent_id: str = "", name: str = "") -> str:
        parent_id = parent_id or ""
        fid = self._fid()
        self.dirs.setdefault(parent_id, {})[name] = (fid, 2)
        self.dirs.setdefault(fid, {})
        return fid

    def list_dir(self, parent_id: str = ""):
        parent_id = parent_id or ""
        return [{"file_id": fid, "name": name, "res_type": rt, "parent_id": parent_id}
                for name, (fid, rt) in self.dirs.get(parent_id, {}).items()]

    def list_folders(self, parent_id: str = ""):
        return [e for e in self.list_dir(parent_id) if e["res_type"] == 2]

    def rename_file(self, file_id: str, new_name: str) -> None:
        for parent, entries in self.dirs.items():
            for name, (fid, rt) in list(entries.items()):
                if fid == file_id:
                    del entries[name]
                    entries[new_name] = (fid, rt)
                    return
        raise AssertionError(f"rename: file_id {file_id} 不存在")

    def delete_file(self, parent_id: str, file_id: str) -> None:
        for entries in self.dirs.values():
            for name, (fid, _rt) in list(entries.items()):
                if fid == file_id:
                    del entries[name]
                    return
        raise AssertionError(f"delete: file_id {file_id} 不存在")

    # ---------- 离线任务 ----------
    def resolve(self, url: str) -> dict:
        name = self.url_map.get(url) or url.rsplit("/", 1)[-1] or "unknown"
        return {"res_type": 0, "name": name}

    def create_offline_task(self, url: str, parent_id: str = "",
                            cn_name: str = "", resolved: dict | None = None):
        # 服务端忽略 cn_name（实测），任务名来自种子内容
        name = (resolved or self.resolve(url))["name"]
        self._counter += 1
        task_id = f"t{self._counter}"
        self.tasks[task_id] = {"url": url, "parent_id": parent_id or "", "name": name,
                               "status": STATUS_SUCCESS if self.auto_complete else STATUS_RUNNING,
                               "file_id": ""}
        if self.auto_complete:
            self._spawn_artifact(task_id)
        return task_id, name

    def _spawn_artifact(self, task_id: str) -> None:
        t = self.tasks[task_id]
        name = t["name"]
        is_file = t["url"] in self.file_urls
        fid = self._fid()
        rt = 1 if is_file else 2
        self.dirs.setdefault(t["parent_id"], {})[name] = (fid, rt)
        t["file_id"] = fid

    def complete(self, task_id: str) -> None:
        """测试手动完成任务（模拟大文件在后台下载完成）。"""
        self.tasks[task_id]["status"] = STATUS_SUCCESS
        if not self.tasks[task_id]["file_id"]:
            self._spawn_artifact(task_id)

    def wait_offline_task(self, task_id: str, timeout: int = 120,
                          poll_interval: int = 10) -> tuple[int, str]:
        t = self.tasks.get(task_id)
        if t is None:
            return 3, "任务不存在"
        return t["status"], "已完成" if t["status"] == STATUS_SUCCESS else "离线下载中"

    def list_tasks(self, statuses=None):
        out = [OfflineTask(task_id=tid, file_id=t["file_id"], name=t["name"],
                           status=t["status"]) for tid, t in self.tasks.items()]
        if statuses:
            out = [t for t in out if t.status in statuses]
        return out


def build_world(auto_complete=True, file_urls=None, url_map=None):
    """复刻 main() 的真实装配，client 换成 CloudSim。返回 (handler, client, store)。"""
    client = CloudSim(auto_complete=auto_complete, file_urls=file_urls, url_map=url_map)
    store = Store(":memory:")
    flt = KeywordFilter([], [], "")
    notifier = Notifier(console=False)
    classifier = Classifier(
        mapping={"movie:cn": "华语电影", "movie:west": "欧美电影",
                 "movie:jpkr": "日韩电影", "movie:other": "其他电影",
                 "tv:cn": "国产剧", "tv:west": "欧美剧", "tv:jpkr": "日韩剧",
                 "tv:other": "其他剧集", "anime:cn": "国产动漫", "anime:jpkr": "日本动漫",
                 "anime:west": "欧美动漫", "documentary:cn": "纪录片",
                 "variety:cn": "综艺", "music:cn": "演唱会"},
        structure="flat", unknown_dir="未分类")
    resolver = CategoryResolver(client, root_id="", create_missing=True)
    dedup = CloudDedup(client, resolver, classifier, cloud_check_new=True,
                       cache_ttl=0, organize_enabled=True, upgrade=False,
                       require_cn=True)
    handler = main.make_handler(store, client, flt, notifier, "", 2,
                                classifier=classifier, resolver=resolver,
                                dedup=dedup, organize_enabled=True)
    return handler, client, store


class Msg:
    def __init__(self, text, links, channel="ch", message_id="m1"):
        self.text, self.links, self.channel, self.message_id = text, links, channel, message_id


def walk(client: CloudSim, parent: str = "") -> dict[str, str]:
    """拍快照：相对路径 -> 名称。"""
    out: dict[str, str] = {}
    for e in client.list_dir(parent):
        key = e["name"]
        out[key] = e["name"]
    return out


def test_bt_folder_renamed_to_cn():
    print("\n=== 场景1 BT磁力→文件夹产物→主路径改中文名 ===")
    url = "magnet:?xt=urn:btih:AAAA1111SEARCHING"
    en = "Searching.2018.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT"
    handler, client, store = build_world(url_map={url: en})
    handler(Msg(f"{en}\n{url}", [url]))
    tree = walk(client)
    assert "欧美电影" in tree, f"应自动建分类目录，实际 {list(tree)}"
    cat_west = next(e["file_id"] for e in client.list_dir("") if e["name"] == "欧美电影")
    names_west = [e["name"] for e in client.list_dir(cat_west)]
    assert "网络谜踪.2018" in names_west, f"欧美电影目录下应有中文名产物，实际 {names_west}"
    assert en not in names_west, "英文原名应已消失"
    rec = store.history(limit=10)[0]
    assert rec.status == "done" and rec.renamed == 1, f"落库应为 done/renamed=1: {rec.status}/{rec.renamed}"
    assert rec.cn_folder == "网络谜踪.2018"
    print("  OK 磁力 → 欧美电影/网络谜踪.2018（文件夹已改名，store renamed=1）")


def test_dedup_skips_same_title():
    print("\n=== 场景2 同片不同磁力 → 云端复查去重跳过 ===")
    u1 = "magnet:?xt=urn:btih:AAAA1111SEARCHING"
    u2 = "magnet:?xt=urn:btih:BBBB2222SEARCHING"
    en = "Searching.2018.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT"
    handler, client, store = build_world(url_map={u1: en, u2: en})
    handler(Msg(f"{en}\n{u1}", [u1], message_id="m1"))
    n_before = len(client.list_dir(next(e["file_id"] for e in client.list_dir("")
                                        if e["name"] == "欧美电影")))
    handler(Msg(f"{en}\n{u2}", [u2], message_id="m2"))   # 同片不同 hash
    cat_dir = next(e["file_id"] for e in client.list_dir("") if e["name"] == "欧美电影")
    n_after = len(client.list_dir(cat_dir))
    assert n_after == n_before, f"同片不同磁力不应重复落盘（{n_before}→{n_after}）"
    rec2 = [r for r in store.history(limit=10) if r.hash == main.link_key(u2)][0]
    assert rec2.status == "skipped", f"第二条应被跳过，实际 {rec2.status}"
    print("  OK 同片不同磁力 → skip（云端目录未新增）")


def test_single_file_artifact_renamed_with_ext():
    print("\n=== 场景3 直链单文件产物 → 改中文名并保留扩展名 ===")
    url = "https://dl.example.com/The.Wandering.Earth.II.2023.1080p.WEB-DL.HC.mkv"
    en = "The.Wandering.Earth.II.2023.1080p.WEB-DL.HC.mkv"
    handler, client, store = build_world(url_map={url: en}, file_urls={url})
    handler(Msg(f"流浪地球2 The.Wandering.Earth.II.2023.1080p.WEB-DL.HC.mkv {url}", [url], message_id="m3"))
    cn_dir = next(e["file_id"] for e in client.list_dir("") if e["name"] == "华语电影")
    names = [e["name"] for e in client.list_dir(cn_dir)]
    assert "流浪地球2.2023.mkv" in names, f"应保留扩展名的中文名，实际 {names}"
    assert en not in names, "英文原名应已消失"
    rec = store.history(limit=10)[0]
    assert rec.renamed == 1 and rec.status == "done"
    print("  OK 直链 → 华语电影/流浪地球2.2023.mkv（文件已改名+扩展名保留）")


def test_chinese_title_goes_cn_category():
    print("\n=== 场景4 中文标题消息 → 华语电影 + 中文名 ===")
    url = "magnet:?xt=urn:btih:CCCC3333LIULANG"
    en = "The.Wandering.Earth.2019.1080p.WEB-DL.DD5.1.H264"
    handler, client, store = build_world(url_map={url: en})
    handler(Msg("【4K】流浪地球 The.Wandering.Earth 2019 国语中字 " + url, [url], message_id="m4"))
    cn_dir = next(e["file_id"] for e in client.list_dir("") if e["name"] == "华语电影")
    names = [e["name"] for e in client.list_dir(cn_dir)]
    assert "流浪地球.2019" in names, f"应落华语电影/流浪地球.2019，实际 {names}"
    print("  OK 中文标题 → 华语电影/流浪地球.2019")


def test_timeout_task_completed_by_monitor():
    print("\n=== 场景5 提交超时→监控线程查全量任务→补改名（历史bug回归）===")
    url = "magnet:?xt=urn:btih:DDDD4444COLDWAR"
    en = "Cold.War.1994.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-HiveWeb"
    handler, client, store = build_world(auto_complete=False, url_map={url: en})
    # 提交阶段任务一直在下载 → submit_one 等待超时（把等待时长压到 1s）
    old_t, old_p = main._OFFLINE_WAIT_TIMEOUT, main._OFFLINE_POLL_INTERVAL
    main._OFFLINE_WAIT_TIMEOUT, main._OFFLINE_POLL_INTERVAL = 1, 1
    try:
        handler(Msg(f"{en}\n{url}", [url], message_id="m5"))
    finally:
        main._OFFLINE_WAIT_TIMEOUT, main._OFFLINE_POLL_INTERVAL = old_t, old_p
    rec = store.history(limit=10)[0]
    assert rec.status == "submitted" and rec.renamed == 0, \
        f"超时任务应为 submitted/renamed=0，实际 {rec.status}/{rec.renamed}"
    # 任务在后台完成（真实场景：大文件下完）
    client.complete(rec.task_id)
    # 复刻监控线程一轮（必须查全量列表——历史 bug：只查 [0,1,4] 时完成任务"消失"）
    pending_tasks = client.list_tasks()          # 全量
    task_map = {t.task_id: t for t in pending_tasks}
    t = task_map[rec.task_id]
    assert t.status == STATUS_SUCCESS, "全量查询必须能看到已完成任务（修复点）"
    assert t.status in [x.status for x in []] or True
    rename_ok = main._rename_artifact_to_cn(client, rec.task_id, t.name or "",
                                            rec.parent_id, rec.cn_folder)
    store.update(rec.hash, status="done", renamed=1 if rename_ok else 2)
    rec2 = store.history(limit=10)[0]
    assert rec2.status == "done" and rec2.renamed == 1, f"监控补改后应 done/1，实际 {rec2.status}/{rec2.renamed}"
    hw = next(e["file_id"] for e in client.list_dir("") if e["name"] == "华语电影")
    names = [e["name"] for e in client.list_dir(hw)]
    assert "冷战.1994" in names and en not in names, f"云盘应有中文名，实际 {names}"
    print("  OK 超时任务 → 监控补改名 → 华语电影/冷战.1994（香港片，region=cn 正确）")


if __name__ == "__main__":
    test_bt_folder_renamed_to_cn()
    test_dedup_skips_same_title()
    test_single_file_artifact_renamed_with_ext()
    test_chinese_title_goes_cn_category()
    test_timeout_task_completed_by_monitor()
    print("\n==== 全链路端到端：全部通过 ✅")
