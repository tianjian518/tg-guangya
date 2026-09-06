"""监控改名路径回归测试。

用户最尖锐的反馈：「重命名这条路完全没有搞通」「频道里的链接直接落盘成英文」。
本测试验证 main._rename_folder_to_cn 这条改名链路真的能工作：
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
from main import _rename_folder_to_cn
from core.guangya import GuangyaError
from core.store import Store, MagnetRecord


class FakeClient:
    """最小光鸭替身：维护 (parent_id -> {folder_name: file_id})。

    rename_file 默认真改名；测试可把它替换成 no-op 来模拟「接口静默忽略」。
    list_tasks 默认返回空 → 改名走「按英文原名匹配」兜底路径（最常见的真实情况）。
    """
    def __init__(self):
        self.tree = {"": {}}          # parent_id -> {name: fid}
        self._n = 0
        self.list_tasks_return = []

    def _fid(self):
        self._n += 1
        return f"f{self._n}"

    def seed_folder(self, parent_id, name):
        fid = self._fid()
        self.tree.setdefault(parent_id, {})[name] = fid
        return fid

    def list_dir(self, parent_id=""):
        parent_id = parent_id or ""
        return [{"file_id": fid, "name": name, "res_type": 2, "parent_id": parent_id}
                for name, fid in self.tree.get(parent_id, {}).items()]

    def rename_file(self, file_id, new_name):
        # 默认真正改名；子类/test 可覆盖成 no-op
        for sub in self.tree.values():
            for name, fid in list(sub.items()):
                if fid == file_id:
                    sub[new_name] = sub.pop(name)
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
    ok = _rename_folder_to_cn(c, "task1", "Cold.War.1994", "PARENT", "冷战.1994")
    assert ok is True, f"改名应成功，实际返回 {ok!r}"
    names = [e["name"] for e in c.list_dir("PARENT")]
    assert "冷战.1994" in names, f"校验失败：目录里没有中文名，实际 {names}"
    assert "Cold.War.1994" not in names, "英文原名未被替换"
    print("  OK 英文文件夹 → 冷战.1994（校验生效）")


def test_rename_missing_folder_returns_false():
    print("\n=== 找不到英文文件夹 → 返回 False（不静默成功）===")
    c = FakeClient()
    c.seed_folder("PARENT", "SomethingElse.2023")     # 名字对不上
    ok = _rename_folder_to_cn(c, "task2", "Cold.War.1994", "PARENT", "冷战.1994")
    assert ok is False, f"找不到文件夹时应返回 False，实际 {ok!r}"
    print("  OK 英文名对不上 → False")


def test_rename_silently_ignored_returns_false():
    print("\n=== 接口静默忽略改名 → 校验失败返回 False ===")
    c = SilentClient()
    c.seed_folder("PARENT", "Cold.War.1994")
    ok = _rename_folder_to_cn(c, "task3", "Cold.War.1994", "PARENT", "冷战.1994")
    assert ok is False, f"云端忽略改名时应返回 False，实际 {ok!r}"
    print("  OK 改名被静默忽略 → False（可据此标记 renamed=2）")


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
    # 复刻 main.start_task_monitor._loop 的成功分支（用真实 _rename_folder_to_cn + store.update）
    if t.status == main.GuangyaClient.STATUS_SUCCESS:
        r = store.history(limit=200)
        for r0 in r:
            if r0.task_id == "T1" and r0.renamed not in (1, 2) and r0.cn_folder and r0.parent_id:
                rename_ok = _rename_folder_to_cn(c, "T1", t.name or "", r0.parent_id, r0.cn_folder)
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
            rename_ok = _rename_folder_to_cn(cs, "T2", t2.name or "", r0.parent_id, r0.cn_folder)
            store.update(r0.hash, renamed=1 if rename_ok else 2)
    after2 = [x for x in store.history(limit=200) if x.hash == "h2"][0]
    assert after2.renamed == 2, f"失败后 renamed 应为 2，实际 {after2.renamed}"
    print("  OK 改名失败 → renamed=2")


if __name__ == "__main__":
    test_rename_by_original_name()
    test_rename_missing_folder_returns_false()
    test_rename_silently_ignored_returns_false()
    test_monitor_persists_renamed_flag()
    print("\n==== 监控改名路径：全部通过 ✅")
