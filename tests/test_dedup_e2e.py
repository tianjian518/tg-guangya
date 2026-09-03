"""云端查重端到端测试：用 FakeGuangya 模拟光鸭目录，验证两级去重决策。

不依赖真实光鸭服务器（沙箱网络受限），全部走内存假对象。
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__) + "/..")

from core.guangya import GuangyaClient
from core.store import Store, MagnetRecord
from core.config import AppConfig
from core.classifier import Classifier
from core.organizer import CategoryResolver
from core.dedup import CloudDedup, title_core, names_match, quality_score


class FakeGuangya:
    """内存版光鸭：dirs[dir_id] = {name, parent, files:{name:size}, subdirs:{name:dir_id}}"""
    def __init__(self):
        self.dirs = {"": {"name": "root", "parent": None, "files": {}, "subdirs": {}}}
        self.next = 1

    def _new_id(self):
        self.next += 1
        return f"d{self.next}"

    def create_folder(self, parent="", name=""):
        parent = parent or ""
        d = self.dirs[parent]
        if name in d["subdirs"]:
            return d["subdirs"][name]
        nid = self._new_id()
        self.dirs[nid] = {"name": name, "parent": parent, "files": {}, "subdirs": {}}
        d["subdirs"][name] = nid
        return nid

    def list_folders(self, parent=""):
        parent = parent or ""
        d = self.dirs[parent]
        return [{"file_id": sid, "name": self.dirs[sid]["name"], "parent_id": parent}
                for sid in d["subdirs"].values()]

    def list_dir(self, parent=""):
        parent = parent or ""
        d = self.dirs[parent]
        out = [{"file_id": sid, "name": self.dirs[sid]["name"], "size": 0, "res_type": 2, "md5": "", "parent_id": parent}
               for sid in d["subdirs"].values()]
        for fn, sz in d["files"].items():
            out.append({"file_id": "f_" + fn, "name": fn, "size": sz, "res_type": 1, "md5": "", "parent_id": parent})
        return out


def build(cfg_overrides=None, cloud_files=None, upgrade=False):
    """构造一个测试实例：根目录 + 自动分类子树 + 可选预置云端文件。"""
    cfg = AppConfig.load("config.yaml")
    if cfg_overrides:
        for k, v in cfg_overrides.items():
            setattr(cfg, k, v)
    client = FakeGuangya()
    # 转存根目录
    root = client.create_folder("", "TG转存")
    classifier = Classifier(mapping=cfg.organize.mapping or None,
                            structure=cfg.organize.structure, unknown_dir=cfg.organize.unknown_dir)
    resolver = CategoryResolver(client, root_id=root, create_missing=True)
    # 预置云端已有文件（模拟用户之前转存过的）。cloud_files 用真实分类目录名（如「华语电影」）
    if cloud_files:
        for cat, fname in cloud_files:
            pid, _ = resolver.resolve(cat)
            client.dirs[pid]["files"][fname] = 1024
    db = ":memory:"
    store = Store(db)
    dedup = CloudDedup(client, resolver, classifier,
                       cloud_check_new=cfg.dedup.cloud_check_new,
                       cache_ttl=cfg.dedup.cache_ttl,
                       organize_enabled=cfg.organize.enabled,
                       upgrade=upgrade)
    return client, resolver, classifier, store, dedup


def test_cases():
    print("=== 两级去重决策测试 ===\n")
    results = []

    # 1) 全新资源、云端空 → transfer
    c, r, clf, st, dd = build()
    d = dd.decide("hash_new", "流浪地球2 2023 4K 国语中字", st)
    results.append(("全新资源/云端空", "transfer", d.action))

    # 2) 不同磁力、但云端分类目录已有同名文件 → skip_exists（跨磁重复防护）
    c, r, clf, st, dd = build(cloud_files=[("华语电影", "流浪地球2.2023.1080p.BluRay.mkv")])
    d = dd.decide("hash_other_magnet", "【电影】流浪地球2 The Wandering Earth II 2023 4K HDR 国语中字", st)
    results.append(("不同磁力/云端已有同名", "skip_exists", d.action))

    # 3) 本地记录 done + 云端仍在 → skip_exists（奥本海默判为欧美电影，文件也放欧美电影目录）
    c, r, clf, st, dd = build(cloud_files=[("欧美电影", "奥本海默.2023.1080p.mkv")])
    st.add(MagnetRecord(hash="h_opp", status="done", title="奥本海默 2023"))
    d = dd.decide("h_opp", "奥本海默 Oppenheimer 2023 BluRay 英语中字", st)
    results.append(("本地done/云端仍在", "skip_exists", d.action))

    # 4) 本地记录 done + 云端被删 → retransfer
    c, r, clf, st, dd = build()  # 云端无该文件
    st.add(MagnetRecord(hash="h_del", status="done", title="沙丘2 2024"))
    d = dd.decide("h_del", "沙丘2 Dune Part Two 2024 4K 国语中字", st)
    results.append(("本地done/云端已删", "retransfer", d.action))

    # 5) 本地记录 failed（非 done）→ 当新资源，云端空 → transfer
    c, r, clf, st, dd = build()
    st.add(MagnetRecord(hash="h_fail", status="failed", title="某片 2024"))
    d = dd.decide("h_fail", "某片 2024 1080P 国语", st)
    results.append(("本地failed/云端空", "transfer", d.action))

    # 6) 未开启云端复查(cloud_check_new=False) → 不同磁力也算 transfer
    c, r, clf, st, dd = build({"dedup": __import__("types").SimpleNamespace(
        cloud_check_new=False, cache_ttl=300.0)},
        cloud_files=[("华语电影", "流浪地球2.2023.1080p.mkv")])
    d = dd.decide("hash_X", "流浪地球2 2023 4K 国语中字", st)
    results.append(("关云端复查/同片不同磁", "transfer", d.action))

    # 7) 未开启自动分类 → 单目录根查重
    cfg = AppConfig.load("config.yaml")
    cfg.organize.enabled = False
    c = FakeGuangya(); root = c.create_folder("", "TG转存"); c.dirs[root]["files"]["繁花.2023.1080p.mkv"] = 1
    clf = Classifier(); r2 = CategoryResolver(c, root_id=root, create_missing=False)
    st = Store(":memory:")
    dd2 = CloudDedup(c, r2, clf, cloud_check_new=True, organize_enabled=False)
    d = dd2.decide("h_fanhua", "繁花 更新至18集 1080P 国语中字", st)
    results.append(("未分类/根目录已有同名", "skip_exists", d.action))

    # 8) 追剧：已转 1~5 集，第6集新链接(新 btih) → transfer（绝不被第5集误杀）
    c, r, clf, st, dd = build(cloud_files=[
        ("国产剧", "庆余年 第01集.mp4"), ("国产剧", "庆余年 第02集.mp4"),
        ("国产剧", "庆余年 第03集.mp4"), ("国产剧", "庆余年 第04集.mp4"),
        ("国产剧", "庆余年 第05集.mp4")])
    d = dd.decide("hash_ep06", "庆余年 第06集 1080p 国语中字", st)
    results.append(("追剧/第6集新链接", "transfer", d.action))

    # 9) 追剧：第5集被不同磁力重复发布 → skip_exists（同集去重仍生效）
    c, r, clf, st, dd = build(cloud_files=[("国产剧", "庆余年 第05集.mp4")])
    d = dd.decide("hash_ep05_other", "【剧集】庆余年 第05集 2160p 高清", st)
    results.append(("追剧/第5集重复发布", "skip_exists", d.action))

    # 10) 集数格式等价：云端 S01E06，新链接「第6集」→ skip_exists
    c, r, clf, st, dd = build(cloud_files=[("国产剧", "庆余年 S01E06.mp4")])
    d = dd.decide("hash_ep06b", "庆余年 第06集 1080p", st)
    results.append(("追剧/S01E06=第6集", "skip_exists", d.action))

    # 11) 洗版开 + 云端有 1080P，新链接 4K（更优）→ upgrade（替换旧版）
    c, r, clf, st, dd = build(upgrade=True, cloud_files=[("华语电影", "流浪地球2 2023 1080p 国语.mkv")])
    d = dd.decide("hash_4k", "流浪地球2 2023 4K 2160p 国语中字", st)
    results.append(("洗版/4K替换1080P", "upgrade", d.action))
    results.append(("洗版/携带旧版fileId", "f_流浪地球2 2023 1080p 国语.mkv", d.replace_file_id))

    # 12) 洗版关 + 云端有 1080P，新链接 4K → 仍按旧逻辑 skip_exists
    c, r, clf, st, dd = build(upgrade=False, cloud_files=[("华语电影", "流浪地球2 2023 1080p 国语.mkv")])
    d = dd.decide("hash_4k2", "流浪地球2 2023 4K 2160p 国语中字", st)
    results.append(("洗版未开/4K不替换", "skip_exists", d.action))

    # 13) 洗版开 + 云端有 4K，新链接 1080P（更差）→ skip_exists（不降级）
    c, r, clf, st, dd = build(upgrade=True, cloud_files=[("华语电影", "流浪地球2 2023 4K 2160p.mkv")])
    d = dd.decide("hash_1080", "流浪地球2 2023 1080p 国语中字", st)
    results.append(("洗版/不降级1080P", "skip_exists", d.action))

    # 14) 洗版开 + 云端有 1080P，新链接同质量 1080P → skip_exists（无更优不替换）
    c, r, clf, st, dd = build(upgrade=True, cloud_files=[("华语电影", "流浪地球2 2023 1080p.mkv")])
    d = dd.decide("hash_1080b", "流浪地球2 2023 1080p 国语中字", st)
    results.append(("洗版/同质量不替换", "skip_exists", d.action))

    ok = 0
    for name, exp, got in results:
        hit = "OK " if exp == got else "BAD"
        if exp == got: ok += 1
        print(f"  {hit} {name:<26} 期望={exp:<12} 实际={got}")
    print(f"\n结果: {ok}/{len(results)} 通过")
    return ok == len(results)


def test_matching():
    print("\n=== 片名匹配单测 ===")
    cases = [
        ("流浪地球2 2023 4K 国语中字", "流浪地球2.2023.1080p.BluRay.mkv", True),
        ("奥本海默 Oppenheimer 2023", "Oppenheimer.2023.1080p.BluRay.mkv", True),
        ("繁花 更新至18集 1080P 国语中字", "繁花.2023.1080p.mkv", True),
        ("鬼灭之刃 柱训练篇 第01集", "鬼灭之刃.柱训练篇.1080p.mkv", True),
        ("庆余年 第06集 1080p", "庆余年 第05集.mp4", False),       # 不同集 → 不误判为同片（追剧核心）
        ("庆余年 第05集 1080p", "庆余年 第05集.mp4", True),        # 同集同名 → 去重
        ("庆余年 S01E06", "庆余年 第06集.mp4", True),              # SxxExx 与 第X集 等价
        ("庆余年 第2季第3集", "庆余年 S02E03.mp4", True),          # 季+集 等价
        ("流浪地球2", "流浪地球 (2005).1080p.mkv", False),   # 续集 vs 前作
        ("沙丘", "沙丘2 2024 4K.mkv", False),                  # 沙丘 vs 沙丘2
        ("完全不同的片子", "另一部电影 2024.mkv", False),
    ]
    ok = 0
    for a, b, exp in cases:
        got = names_match(a, b)
        hit = "OK " if got == exp else "BAD"
        if got == exp: ok += 1
        print(f"  {hit} names_match({a!r}, {b!r}) = {got} (期望 {exp})")
    print(f"\n匹配单测: {ok}/{len(cases)} 通过")
    return ok == len(cases)


def test_quality():
    print("\n=== 质量评分单测（洗版择优依据）===")
    cases = [
        ("流浪地球2 2023 4K 2160p 国语中字", 120),   # 4K 命中 2160p/4k 取 120，无额外加成项
        ("电影 1080p BluRay REMUX", 60 + 15),                          # 1080P + remux
        ("影片 720p HD", 35),
        ("普通 480p", 15),
        ("无分辨率信息", 0),
        ("4K HDR10 Atmos HEVC 10bit", 120 + 8 + 10 + 15 + 4),          # 全部叠加
    ]
    ok = 0
    for title, exp in cases:
        got = quality_score(title)
        hit = "OK " if got == exp else "BAD"
        if got == exp: ok += 1
        print(f"  {hit} quality_score({title!r}) = {got} (期望 {exp})")
    # 关键比较：4K 必须高于 1080P
    assert quality_score("X 4K 2160p") > quality_score("X 1080p"), "4K 应高于 1080P"
    assert quality_score("X 1080p REMUX") > quality_score("X 1080p"), "REMUX 应高于同分辨率普通版"
    print(f"\n质量评分单测: {ok}/{len(cases)} 通过")
    return ok == len(cases)


if __name__ == "__main__":
    a = test_cases()
    b = test_matching()
    q = test_quality()
    print("\n==== 结论:", "全部通过 ✅" if (a and b and q) else "存在失败 ❌")
    sys.exit(0 if (a and b) else 1)
