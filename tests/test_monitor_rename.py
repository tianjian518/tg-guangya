"""监控改名路径回归测试。

用户最尖锐的反馈：「重命名这条路完全没有搞通」「频道里的链接直接落盘成英文」。
本测试验证 main._rename_artifact_to_cn 这条改名链路真的能工作：
  1. 离线下载生成英文外层文件夹后，能按英文原名定位 → 改中文名 → 重新列举校验生效；
  2. 文件夹找不到（英文原名对不上）时返回 False（不静默成功）；
  3. 云端接口静默忽略改名（rename 后列举仍无中文名）时返回 False，便于标记 renamed=2；
  4. 与 SQLite store 联动：成功后 renamed 落库为 1，失败为 2（监控线程补改名即走这条）。

注意：真实的云端 rename 接口是否支持、需哪些字段，仍要用户在自己环境跑
tools/diagnose_rename.py 用真实 token 验证——本测试验证的是「一旦接口可用，
改名+校验+落库」这段逻辑正确，不再依赖未经证实的假设。

直接跑：python3 tests/test_monitor_rename.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from main import _rename_artifact_to_cn
from core.guangya import GuangyaError
from core.store import Store, MagnetRecord


class FakeClient:
    """最小光鸭替身：维护 (parent_id -> {folder_name: file_id})。

    rename_file 默认真改名；测试可把它替换成 no-op 来模拟「接口静默忽略」。
    list_tasks 默认返回空 → 改名走「按英文原名匹配」兜底路径（最常见的真实情况）。
    """
    def __init__(self):
        self.tree = {"": {}}          # parent_id -> {name: fid}
        self.kinds = {}               # (parent_id, name) -> res_type（1=文件 2=文件夹）
        self._n = 0
        self.list_tasks_return = []

    def _fid(self):
        self._n += 1
        return f"f{self._n}"

    def seed_folder(self, parent_id, name):
        fid = self._fid()
        self.tree.setdefault(parent_id, {})[name] = fid
        self.kinds[(parent_id or "", name)] = 2
        return fid

    def seed_file(self, parent_id, name):
        """单文件产物（HTTP 直链/单文件种子 → res_type=1，名字带扩展名）。"""
        fid = self._fid()
        self.tree.setdefault(parent_id, {})[name] = fid
        self.kinds[(parent_id or "", name)] = 1
        return fid

    def list_dir(self, parent_id=""):
        parent_id = parent_id or ""
        out = []
        for name, fid in self.tree.get(parent_id, {}).items():
            rt = self.kinds.get((parent_id, name), 2)
            out.append({"file_id": fid, "name": name, "res_type": rt,
                        "parent_id": parent_id})
        return out

    def rename_file(self, file_id, new_name):
        # 默认真正改名；子类/test 可覆盖成 no-op。改名后 res_type 类型跟随迁移。
        for parent_id, sub in self.tree.items():
            for name, fid in list(sub.items()):
                if fid == file_id:
                    sub[new_name] = sub.pop(name)
                    rt = self.kinds.pop((parent_id, name), None)
                    if rt is not None:
                        self.kinds[(parent_id, new_name)] = rt
                    return True
        return False

    def list_tasks(self, statuses=None):
        return self.list_tasks_return


class SilentClient(FakeClient):
    """rename 后云端仍显示旧名 —— 模拟接口静默忽略改名。"""
    def rename_file(self, file_id, new_name):
        return True  # 假装成功，实际不改名


def test_rename_by_original_name():
    print("\n=== 按英文原名定位并改中文名，且校验生效 ===")
    c = FakeClient()
    c.seed_folder("PARENT", "Cold.War.1994")          # 离线下载落盘的英文文件夹
    ok = _rename_artifact_to_cn(c, "task1", "Cold.War.1994", "PARENT", "冷战.1994")
    assert ok is True, f"改名应成功，实际返回 {ok!r}"
    names = [e["name"] for e in c.list_dir("PARENT")]
    assert "冷战.1994" in names, f"校验失败：目录里没有中文名，实际 {names}"
    assert "Cold.War.1994" not in names, "英文原名未被替换"
    print("  OK 英文文件夹 → 冷战.1994（校验生效）")


def test_rename_missing_folder_returns_false():
    print("\n=== 找不到英文文件夹 → 返回 False（不静默成功）===")
    c = FakeClient()
    c.seed_folder("PARENT", "SomethingElse.2023")     # 名字对不上
    ok = _rename_artifact_to_cn(c, "task2", "Cold.War.1994", "PARENT", "冷战.1994")
    assert ok is False, f"找不到文件夹时应返回 False，实际 {ok!r}"
    print("  OK 英文名对不上 → False")


def test_rename_silently_ignored_returns_false():
    print("\n=== 接口静默忽略改名 → 校验失败返回 False ===")
    c = SilentClient()
    c.seed_folder("PARENT", "Cold.War.1994")
    ok = _rename_artifact_to_cn(c, "task3", "Cold.War.1994", "PARENT", "冷战.1994")
    assert ok is False, f"云端忽略改名时应返回 False，实际 {ok!r}"
    print("  OK 改名被静默忽略 → False（可据此标记 renamed=2）")


def test_rename_single_file_artifact():
    """真实网盘观察：HTTP 直链/单文件种子落盘是【单个文件】而非文件夹。

    旧实现只匹配 res_type==2 的文件夹 → 单文件产物永远改不了名
    （用户盘里 tg转存/华语电影/The.Wandering.Earth.II...mkv 就是实例）。
    新实现匹配文件并保留原扩展名。
    """
    print("\n=== 单文件产物改名（保留原扩展名）===")
    c = FakeClient()
    c.seed_file("PARENT", "The.Wandering.Earth.II.2023.1080p.WEB-DL.HC.mkv")
    ok = _rename_artifact_to_cn(
        c, "task4", "The.Wandering.Earth.II.2023.1080p.WEB-DL.HC.mkv",
        "PARENT", "流浪地球2.2023")
    assert ok is True, f"单文件改名应成功，实际 {ok!r}"
    names = [e["name"] for e in c.list_dir("PARENT")]
    assert "流浪地球2.2023.mkv" in names, f"应保留扩展名，实际 {names}"
    print("  OK 单文件 → 流浪地球2.2023.mkv（扩展名保留）")


def test_rename_via_task_file_id():
    """离线任务返回 file_id 时优先用它定位（实测成功任务都带 file_id）。"""
    print("\n=== 用任务 file_id 定位产物改名 ===")
    c = FakeClient()
    fid = c.seed_file("PARENT", "Some.Movie.2024.1080p.WEB-DL.mkv")
    from core.guangya import OfflineTask
    c.list_tasks_return = [OfflineTask(task_id="T9", file_id=fid,
                                       name="Some.Movie.2024.1080p.WEB-DL.mkv",
                                       status=2)]
    ok = _rename_artifact_to_cn(c, "T9", "Some.Movie.2024.1080p.WEB-DL.mkv",
                                "PARENT", "某电影.2024")
    assert ok is True, f"file_id 定位改名应成功，实际 {ok!r}"
    names = [e["name"] for e in c.list_dir("PARENT")]
    assert "某电影.2024.mkv" in names, f"实际 {names}"
    print("  OK file_id 定位 → 某电影.2024.mkv")


def test_monitor_persists_renamed_flag():
    print("\n=== 监控补改名：store renamed 落库 1/2 ===")
    store = Store(":memory:")
    # 1) 成功路径
    rec = MagnetRecord(hash="h1", title="冷战 1994", status="submitted",
                       task_id="T1", category="华语电影",
                       parent_id="PARENT", cn_folder="冷战.1994", renamed=0)
    store.add(rec)
    c = FakeClient()
    c.seed_folder("PARENT", "Cold.War.1994")
    t = type("T", (), {"task_id": "T1", "name": "Cold.War.1994",
                       "status": main.GuangyaClient.STATUS_SUCCESS, "file_id": "",
                       "message": ""})()
    # 复刻 main.start_task_monitor._loop 的成功分支（用真实 _rename_artifact_to_cn + store.update）
    if t.status == main.GuangyaClient.STATUS_SUCCESS:
        r = store.history(limit=200)
        for r0 in r:
            if r0.task_id == "T1" and r0.renamed not in (1, 2) and r0.cn_folder and r0.parent_id:
                rename_ok = _rename_artifact_to_cn(c, "T1", t.name or "", r0.parent_id, r0.cn_folder)
                store.update(r0.hash, renamed=1 if rename_ok else 2)
    after = store.history(limit=200)[0]
    assert after.renamed == 1, f"成功后 renamed 应为 1，实际 {after.renamed}"
    print("  OK 改名成功 → renamed=1")

    # 2) 失败路径（接口静默忽略）
    rec2 = MagnetRecord(hash="h2", title="某片 2024", status="submitted",
                        task_id="T2", category="欧美电影",
                        parent_id="PARENT", cn_folder="某片.2024", renamed=0)
    store.add(rec2)
    cs = SilentClient()
    cs.seed_folder("PARENT", "X.2024")
    t2 = type("T", (), {"task_id": "T2", "name": "X.2024",
                        "status": main.GuangyaClient.STATUS_SUCCESS, "file_id": "",
                        "message": ""})()
    r = store.history(limit=200)
    for r0 in r:
        if r0.task_id == "T2" and r0.renamed not in (1, 2) and r0.cn_folder and r0.parent_id:
            rename_ok = _rename_artifact_to_cn(cs, "T2", t2.name or "", r0.parent_id, r0.cn_folder)
            store.update(r0.hash, renamed=1 if rename_ok else 2)
    after2 = [x for x in store.history(limit=200) if x.hash == "h2"][0]
    assert after2.renamed == 2, f"失败后 renamed 应为 2，实际 {after2.renamed}"
    print("  OK 改名失败 → renamed=2")


if __name__ == "__main__":
    test_rename_by_original_name()
    test_rename_missing_folder_returns_false()
    test_rename_silently_ignored_returns_false()
    test_rename_single_file_artifact()
    test_rename_via_task_file_id()
    test_monitor_persists_renamed_flag()
    print("\n==== 监控改名路径：全部通过 ✅")
