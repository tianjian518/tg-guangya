"""光鸭分享链接转存链路测试。

覆盖两层：
  A. 协议层（FakeApi 拦截 _api_post，验证请求 body / 业务码处理 / cursor 翻页）
  B. 落盘层（ShareSim 模拟网盘，验证 submit_share_one 的中文命名与剧集收纳）

API 结构依据（2026-09 光鸭 Web 前端 bundle 逆向）：
  - get_share_access_token {shareId, code}，209 = 提取码错误
  - get_share_summary {shareId}，200/201 = 失效，202 = 过期
  - get_share_page_files_list {accessToken, parentId, pageSize, orderBy, sortType, cursor?}
    响应 {list, hasMore, cursor}（前端 by hook 的 cursor 分页）
  - restore_share {accessToken, fileIds, parentId, shareCode?} → data.taskId

直接跑：python3 tests/test_share_restore.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from core.guangya import (
    GuangyaBizError,
    GuangyaClient,
    parse_share_url,
)
from adapters.web_scraper import extract_links, link_key


# ======================================================================
# A0. parse_share_url / extract_links / link_key（纯函数，无网络）
# ======================================================================

def test_parse_share_url_forms():
    u = parse_share_url("https://www.guangyapan.com/share/AbC123")
    assert u == {"share_id": "AbC123", "code": "", "share_code": ""}, u

    # 官方短链 /s/<数字>_<串>（2026-09 社区真实样本）
    u = parse_share_url("https://www.guangyapan.com/s/1894410604530630727_aeXsY5wocgzRgFTv")
    assert u and u["share_id"] == "1894410604530630727_aeXsY5wocgzRgFTv", u
    # 短链 + 提取码（无忧启动论坛样本：?code=jiif 提取码:jiif）
    u = parse_share_url("链接:https://www.guangyapan.com/s/189xxxx726g3wfdj?code=jiif 提取码:jiif")
    assert u and u["share_id"] == "189xxxx726g3wfdj" and u["code"] == "jiif", u

    u = parse_share_url("http://guangyapan.com/share/xYz?code=8848")
    assert u["share_id"] == "xYz" and u["code"] == "8848" and u["share_code"] == "", u

    u = parse_share_url("https://app.guangyapan.com/share/abc?shareCode=kouling")
    assert u["share_code"] == "kouling" and u["code"] == "", u

    # 两者都带（提取码 + 口令）
    u = parse_share_url("https://www.guangyapan.com/share/a1?code=1&shareCode=2")
    assert u == {"share_id": "a1", "code": "1", "share_code": "2"}, u

    # 塞在长文本里
    text = "更新啦！「某剧 4K」https://www.guangyapan.com/share/qWeR?code=520 拿走记得回复"
    u = parse_share_url(text)
    assert u and u["share_id"] == "qWeR" and u["code"] == "520", u

    # 大写域名 / 全角括号截断
    u = parse_share_url("https://WWW.GUANGYAPAN.COM/share/Mix9）。")
    assert u and u["share_id"] == "Mix9", u

    # 非分享链接
    for bad in ("", None, "https://www.guangyapan.com/home",
                "magnet:?xt=urn:btih:" + "a" * 40,
                "https://pan.baidu.com/share/abc"):
        assert parse_share_url(bad) is None, bad


def test_extract_links_and_key():
    text = ("磁力 magnet:?xt=urn:btih:" + "A" * 40 + " 和分享 "
            "https://www.guangyapan.com/share/xyz88?code=66 和短链 "
            "https://www.guangyapan.com/s/1894410604530630727_aeXsY5wocgzRgFTv 拿走")
    links = extract_links(text)
    assert len(links) == 3, links
    assert any(l.startswith("magnet:") for l in links)
    assert any("guangyapan.com/share/xyz88" in l for l in links)
    assert any("/s/1894410604530630727" in l for l in links)

    # link_key：/s/ 与 /share/ 同 id 归一键（同一分享两处写法不应重复处理）
    k1 = link_key("https://www.guangyapan.com/share/xyz88?code=66")
    k2 = link_key("https://guangyapan.com/share/XYZ88")
    assert k1 == k2 == "guangya:xyz88", (k1, k2)
    # 磁力不受影响
    assert link_key("magnet:?xt=urn:btih:" + "A" * 40) == "a" * 40


# ======================================================================
# A1. 协议层：FakeApi 拦截 _api_post
# ======================================================================

class FakeApi(GuangyaClient):
    """按 path 返回预置信封，记录调用序列与 body。

    _api_post 语义对齐真实 _post(raw=False)：code==0 → 返回 data 拆包；
    code!=0 → raise GuangyaBizError（保留 code/msg）。
    """

    def __init__(self, responses: dict[str, list]):
        super().__init__(access_token="atok")
        self.responses = responses      # path -> 信封列表（依次弹出，弹完重复最后一个）
        self.calls: list[tuple[str, dict]] = []

    def _api_post(self, path: str, body: dict):
        self.calls.append((path, body))
        seq = self.responses.get(path)
        if not seq:
            return {}
        env = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(env, Exception):
            raise env
        code = env.get("code", 0)
        if isinstance(code, int) and code != 0:
            raise GuangyaBizError(code, env.get("msg") or "", env)
        return env.get("data")


_SHARE_FILES_PAGE1 = {
    "code": 0, "msg": "ok",
    "data": {"list": [
        {"fileId": "f1", "fileName": "Movie.2024.mkv", "resType": 1, "fileSize": 100},
        {"fileId": "d1", "fileName": "Season 1", "resType": 2, "fileSize": 0},
    ], "hasMore": True, "cursor": "c2"},
}
_SHARE_FILES_PAGE2 = {
    "code": 0, "msg": "ok",
    "data": {"list": [
        {"fileId": "f2", "fileName": "Extra.mp4", "resType": 1, "fileSize": 50},
    ], "hasMore": False},
}


def test_list_share_all_files_pagination():
    c = FakeApi({"/userres/v1/get_share_page_files_list": [
        _SHARE_FILES_PAGE1, _SHARE_FILES_PAGE2,
    ]})
    entries = c.list_share_all_files("stk")
    assert [e["file_id"] for e in entries] == ["f1", "d1", "f2"], entries
    # 翻页带 cursor，首页不带
    bodies = [b for p, b in c.calls if p == "/userres/v1/get_share_page_files_list"]
    assert "cursor" not in bodies[0], bodies[0]
    assert bodies[1].get("cursor") == "c2", bodies[1]
    # 首页 body 必带 accessToken + parentId="" + pageSize
    assert bodies[0]["accessToken"] == "stk" and bodies[0]["parentId"] == ""
    assert bodies[0]["pageSize"] == 100


def test_list_share_all_files_stuck_cursor_guard():
    # 服务端 hasMore 永远 True 且 cursor 重复 → 必须自动停下（不死循环）
    c = FakeApi({"/userres/v1/get_share_page_files_list": [_SHARE_FILES_PAGE1]})
    entries = c.list_share_all_files("stk")
    assert len(entries) == 2


def test_restore_share_body():
    c = FakeApi({"/userres/v1/restore_share": [{"code": 0, "data": {"taskId": "t9"}}]})
    tid = c.restore_share("stk", ["f1", "f2"], "target-dir", share_code="kouling")
    assert tid == "t9"
    path, body = c.calls[-1]
    assert path == "/userres/v1/restore_share"
    assert body == {"accessToken": "stk", "fileIds": ["f1", "f2"],
                    "parentId": "target-dir", "shareCode": "kouling"}, body
    # 不带口令时不传 shareCode 字段
    c.restore_share("stk", ["f1"], "dir")
    assert "shareCode" not in c.calls[-1][1]


def test_save_share_happy_path_protocol():
    c = FakeApi({
        "/userres/v1/get_share_summary": [{"code": 0, "data": {"needCode": False}}],
        "/userres/v1/get_share_access_token": [{"code": 0, "data": {"accessToken": "stk"}}],
        "/userres/v1/get_share_page_files_list": [_SHARE_FILES_PAGE1, _SHARE_FILES_PAGE2],
        "/userres/v1/restore_share": [{"code": 0, "data": {"taskId": "t1"}}],
    })
    c.wait_task = lambda task_id, timeout=30: True  # 任务等待不打真网络

    res = c.save_share("https://www.guangyapan.com/share/ok1?code=9",
                       parent_id="mydir")
    assert res["ok"] is True and res["task_id"] == "t1" and res["files"] == 3, res
    assert {e["file_id"] for e in res["entries"]} == {"f1", "d1", "f2"}
    # 调用顺序：summary → access_token(带 code) → files → restore
    paths = [p for p, _ in c.calls]
    assert paths == ["/userres/v1/get_share_summary",
                     "/userres/v1/get_share_access_token",
                     "/userres/v1/get_share_page_files_list",
                     "/userres/v1/get_share_page_files_list",
                     "/userres/v1/restore_share"], paths
    tok_body = c.calls[1][1]
    assert tok_body == {"shareId": "ok1", "code": "9"}, tok_body


def test_save_share_status_codes():
    for code, expect in ((201, "失效"), (200, "失效"), (202, "过期")):
        c = FakeApi({"/userres/v1/get_share_summary": [
            {"code": code, "msg": f"biz-{code}"}]})
        res = c.save_share("https://www.guangyapan.com/share/dead", "d")
        assert res["ok"] is False and expect in res["message"], (code, res)


def test_access_token_wrong_code_raises_209():
    c = FakeApi({"/userres/v1/get_share_access_token": [
        {"code": 209, "msg": "提取码错误"}]})
    try:
        c.get_share_access_token("s1", "0000")
        raise AssertionError("应抛 GuangyaBizError(209)")
    except GuangyaBizError as exc:
        assert exc.code == 209


# ======================================================================
# B. 落盘层：ShareSim 模拟网盘 + submit_share_one 中文命名
# ======================================================================

class ShareSim(GuangyaClient):
    """内存版光鸭（分享转存子集）：分享内容 → restore 拷进目标目录（新 fileId）。

    目录树与 test_full_pipeline.CloudSim 同构：dirs[parent][name] = (fid, res_type)。
    """

    def __init__(self, shares: dict[str, dict], parent_snapshot: dict | None = None):
        super().__init__(access_token="atok")
        self.shares = shares               # share_id -> {code, share_code, entries, status}
        self.dirs: dict[str, dict[str, tuple[str, int]]] = {"": {}}
        for pid, items in (parent_snapshot or {}).items():
            self.dirs[pid] = {n: (f, t) for n, (f, t) in items.items()}
        self.counter = 1000
        self.rename_log: list[tuple[str, str]] = []
        self.move_log: list[tuple[str, str]] = []

    def _fid(self) -> str:
        self.counter += 1
        return f"n{self.counter}"

    # ---------- 分享协议 ----------
    def get_share_summary(self, share_id: str) -> dict:
        s = self.shares.get(share_id)
        if not s:
            raise GuangyaBizError(201, "分享不存在")
        if s.get("status"):
            raise GuangyaBizError(s["status"], f"status-{s['status']}")
        return {"needCode": bool(s.get("code")), "shareStatus": 0}

    def get_share_access_token(self, share_id: str, code: str = "") -> str:
        s = self.shares.get(share_id)
        if not s:
            raise GuangyaBizError(201, "分享不存在")
        want = s.get("code") or ""
        if want and code != want:
            raise GuangyaBizError(209, "提取码错误")
        return f"stk-{share_id}"

    def list_share_all_files(self, access_token: str, parent_id: str = "") -> list[dict]:
        share_id = access_token.removeprefix("stk-")
        s = self.shares.get(share_id) or {}
        out = []
        for i, e in enumerate(s.get("entries") or []):
            out.append({"file_id": f"{share_id}-{i}", "name": e["name"],
                        "size": e.get("size", 0), "res_type": e.get("res_type", 1),
                        "parent_id": ""})
        return out

    def restore_share(self, access_token: str, file_ids: list[str], parent_id: str,
                      share_code: str = "") -> str:
        share_id = access_token.removeprefix("stk-")
        s = self.shares.get(share_id) or {}
        entries = {f"{share_id}-{i}": e for i, e in enumerate(s.get("entries") or [])}
        bucket = self.dirs.setdefault(parent_id or "", {})
        for fid in file_ids:
            e = entries[fid]
            bucket[e["name"]] = (self._fid(), e.get("res_type", 1))
        return f"rt-{share_id}"

    def wait_task(self, task_id: str, timeout: int = 30) -> bool:
        return True

    # ---------- 网盘操作 ----------
    def list_dir(self, parent_id: str = "", page_size: int = 200) -> list[dict]:
        bucket = self.dirs.get(parent_id or "", {})
        return [{"file_id": fid, "name": name, "size": 0, "res_type": rt,
                 "parent_id": parent_id or ""}
                for name, (fid, rt) in bucket.items()]

    def rename_file(self, file_id: str, new_name: str) -> None:
        self.rename_log.append((file_id, new_name))
        for bucket in self.dirs.values():
            for name, (fid, rt) in list(bucket.items()):
                if fid == file_id:
                    del bucket[name]
                    bucket[new_name] = (fid, rt)
                    return
        raise GuangyaError(f"rename: fileId 不存在 {file_id}")

    def create_folder(self, parent_id: str = "", name: str = "") -> str:
        fid = self._fid()
        self.dirs.setdefault(parent_id or "", {})[name] = (fid, 2)
        return fid

    def move_file(self, file_id: str, target_parent_id: str) -> None:
        self.move_log.append((file_id, target_parent_id))
        src_bucket = src_name = None
        for pid, bucket in self.dirs.items():
            for name, (fid, rt) in bucket.items():
                if fid == file_id:
                    src_bucket, src_name = bucket, name
                    break
            if src_bucket:
                break
        if not src_bucket:
            raise GuangyaError(f"move: fileId 不存在 {file_id}")
        fid, rt = src_bucket.pop(src_name)
        self.dirs.setdefault(target_parent_id, {})[src_name] = (fid, rt)


def _run_share(url: str, sim: ShareSim, title: str, parent="cat-dir"):
    return main.submit_share_one(sim, url, parent, cn_title=title)


def test_submit_share_movie_single_file():
    sim = ShareSim({"mv1": {"entries": [
        {"name": "Avatar.2009.2160p.mkv", "res_type": 1, "size": 9},
    ]}})
    ok, task_id, name, status, rename_ok, cn_folder = _run_share(
        "https://www.guangyapan.com/share/mv1", sim, "阿凡达 2009 4K")
    assert ok and status == "done" and task_id == "rt-mv1", (ok, status, task_id)
    assert cn_folder == "阿凡达.2009", cn_folder
    assert rename_ok is True
    # 单文件保留扩展名改名
    assert ("Avatar.2009.2160p.mkv" not in sim.dirs["cat-dir"])
    assert any(n.startswith("阿凡达.2009.") and n.endswith(".mkv")
               for n in sim.dirs["cat-dir"]), sim.dirs["cat-dir"]


def test_submit_share_episode_into_show_dir():
    sim = ShareSim({"ep1": {"entries": [
        {"name": "Summer.2026.S01E04.2160p.WEB-DL.mp4", "res_type": 1, "size": 9},
    ]}})
    ok, task_id, name, status, rename_ok, cn_folder = _run_share(
        "https://www.guangyapan.com/share/ep1?code=1", sim, "夏季.2026.S01E04")
    assert ok and rename_ok is True, (ok, rename_ok)
    assert cn_folder == "夏季.S01E04", cn_folder
    # 单集收进剧名文件夹（桶按 fileId 组织：先找剧名文件夹的 fid 再看桶内）
    show_fid = next(fid for n, (fid, rt) in sim.dirs["cat-dir"].items()
                    if n == "夏季.2026" and rt == 2)
    show = sim.dirs.get(show_fid) or {}
    assert any(n.startswith("夏季.S01E04") for n in show), sim.dirs
    assert not any("S01E04" in n for n in sim.dirs["cat-dir"]), sim.dirs["cat-dir"]


def test_submit_share_season_folder():
    # BT 整季包形态：分享里是文件夹产物 → 整个文件夹改名
    sim = ShareSim({"sea1": {"entries": [
        {"name": "Qing.Yu.Nian.S02.2160p", "res_type": 2, "size": 0},
    ]}})
    ok, task_id, name, status, rename_ok, cn_folder = _run_share(
        "https://www.guangyapan.com/share/sea1", sim, "庆余年 第二季 4K 全集")
    assert ok and rename_ok is True, (ok, rename_ok)
    names = list(sim.dirs["cat-dir"])
    assert any(n.startswith("庆余年") for n in names), names
    # 文件夹产物没有扩展名 → 不应带 ".mkv" 之类尾巴
    assert all(not n.endswith((".mkv", ".mp4")) for n in names), names


def test_submit_share_multi_entries_collected():
    # 多条目分享：整批收进一个文件夹
    sim = ShareSim({"set1": {"entries": [
        {"name": "Part1.mkv", "res_type": 1},
        {"name": "Part2.mkv", "res_type": 1},
        {"name": "Extras", "res_type": 2},
    ]}})
    ok, task_id, name, status, rename_ok, cn_folder = _run_share(
        "https://www.guangyapan.com/share/set1", sim, "哪吒之魔童闹海 2019")
    assert ok and rename_ok is True, (ok, rename_ok)
    sub_fid = next(fid for n, (fid, rt) in sim.dirs["cat-dir"].items()
                   if n == "哪吒之魔童闹海.2019" and rt == 2)
    sub = sim.dirs.get(sub_fid) or {}
    assert {"Part1.mkv", "Part2.mkv", "Extras"} <= set(sub), sim.dirs


def test_submit_share_needs_code():
    sim = ShareSim({"need1": {"code": "8848", "entries": [
        {"name": "a.mkv", "res_type": 1},
    ]}})
    ok, task_id, name, status, rename_ok, cn_folder = _run_share(
        "https://www.guangyapan.com/share/need1", sim, "某电影 2024")
    assert not ok and status == "error", (ok, status)
    assert "提取码" in name, name


def test_submit_share_wrong_code_in_url():
    sim = ShareSim({"need2": {"code": "8848", "entries": [
        {"name": "a.mkv", "res_type": 1},
    ]}})
    ok, task_id, name, status, rename_ok, cn_folder = _run_share(
        "https://www.guangyapan.com/share/need2?code=0000", sim, "某电影 2024")
    assert not ok and "提取码" in name, (ok, name)


def test_submit_share_invalid_and_expired():
    sim = ShareSim({"dead1": {"status": 201, "entries": []},
                    "dead2": {"status": 202, "entries": []}})
    ok1, _, name1, _, _, _ = _run_share(
        "https://www.guangyapan.com/share/dead1", sim, "失效的分享")
    ok2, _, name2, _, _, _ = _run_share(
        "https://www.guangyapan.com/share/dead2", sim, "过期的分享")
    assert not ok1 and "失效" in name1
    assert not ok2 and "过期" in name2


def test_submit_share_not_share_url():
    sim = ShareSim({})
    ok, _, name, _, _, _ = _run_share("magnet:?xt=urn:btih:" + "a" * 40, sim, "x")
    assert not ok and "不是光鸭分享链接" in name


def test_submit_share_unparsable_title_fallback_name():
    # 标题解析不出有效片名 → build_cn_filename 兜底为「影视资源」，
    # 产物仍按兜底规范名改名（不保留英文原名）
    sim = ShareSim({"raw1": {"entries": [
        {"name": "Some.Show.S01E01.mp4", "res_type": 1},
    ]}})
    ok, task_id, name, status, rename_ok, cn_folder = _run_share(
        "https://www.guangyapan.com/share/raw1", sim, "🔥🔥🔥")
    assert ok and status == "done" and rename_ok is True, (ok, status, rename_ok)
    assert cn_folder == "影视资源", cn_folder
    assert "影视资源.mp4" in sim.dirs["cat-dir"], sim.dirs["cat-dir"]


# ======================================================================
# handler 分流：分享链接走进分享链路（不进 create_offline_task）
# ======================================================================

def test_extract_share_title_from_channel_text():
    """频道消息 = 装饰词 + 标题 + 链接 → 只取链接前最近短语做标题。"""
    f = main._extract_share_title
    u = "https://www.guangyapan.com/s/1943524207843811387_adyP1Y8EdLN_2AaC"

    # 真实频道形态：装饰词 + 换行 + 标题，链接：URL
    t = f("♥♥♥♥♥【国漫】【持续更新，敬请收藏】♥♥♥♥♥\n仙逆，链接：" + u, u)
    assert t == "仙逆", t

    t = f("「李熊猫」，链接：" + u, u)
    assert t == "李熊猫", t

    t = f("「遮天 (2023)」，链接：" + u, u)
    assert t == "遮天 (2023)", t

    # 无装饰词直接发（bot /add 场景）
    t = f("李熊猫 " + u, u)
    assert t == "李熊猫", t

    # 带空格的长标题（空格不是分隔符）
    t = f("指环王 三部曲 加长版 4K原盘REMUX，链接：" + u, u)
    assert t == "指环王 三部曲 加长版 4K原盘REMUX", t

    # 书名号嵌套英文名
    t = f("DC 系列电影蓝光原盘，链接：" + u, u)
    assert t == "DC 系列电影蓝光原盘", t

    # 链接在文本中间 / 纯装饰词兜底原文
    t = f("看这个 " + u + " 拿走不谢", u)
    assert t == "看这个", t


def test_extract_share_title_fallbacks():
    f = main._extract_share_title
    u = "https://www.guangyapan.com/s/123_abc"
    assert f("", u) == ""
    assert f("没有链接的普通文本", u) == "没有链接的普通文本"
    # 标题就是链接本身 → 兜底原文
    assert f(u, u) == u


def test_handler_routes_share_links():
    sim = ShareSim({"r1": {"entries": [
        {"name": "Avatar.2009.mkv", "res_type": 1},
    ]}})
    sim.create_offline_task = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("分享链接不应走离线下载"))

    store = main.Store(":memory:")
    handler = main.make_handler(
        store, sim, main.KeywordFilter(include=[], exclude=[]),
        main.Notifier(console=False), parent_id="root",
        max_retries=1, classifier=None, resolver=None, dedup=None,
        organize_enabled=False,
    )
    msg = main.BotMessage(
        links=["https://www.guangyapan.com/share/r1"],
        text="分享一部电影 阿凡达 2009 https://www.guangyapan.com/share/r1",
        channel="test", message_id="m1",
    )
    handler(msg)
    recs = store.history(limit=5)
    assert recs and recs[0].status == "done", recs
    assert recs[0].hash == "guangya:r1", recs[0]


def test_dedup_key_stable_for_share_urls():
    # handler 分流前提：link_key 对同一分享的不同写法产生同一主键（否则去重漏）
    assert (link_key("https://www.guangyapan.com/share/r1?code=1")
            == link_key("https://guangyapan.com/share/r1"))


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
            except Exception as exc:  # noqa: BLE001
                fails += 1
                import traceback
                print(f"❌ {name}: {exc}")
                traceback.print_exc()
    if fails:
        raise SystemExit(f"{fails} 个测试失败")
    print("全部通过 ✅")
