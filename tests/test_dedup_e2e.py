"""云端查重端到端测试：用 FakeGuangya 模拟光鸭目录，验证两级去重决策。

不依赖真实光鸭服务器（沙箱网络受限），全部走内存假对象。
"""
import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
BASE = Path(__file__).resolve().parent.parent

from core.guangya import GuangyaClient
from core.store import Store, MagnetRecord, TitleRecord
from core.config import AppConfig
from core.classifier import Classifier
from core.organizer import CategoryResolver
from core.dedup import CloudDedup, title_core, names_match, quality_score
from core.ident import analyze as ident_analyze, norm as ident_norm


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


def build(cfg_overrides=None, cloud_files=None, cloud_dirs=None,
          root_files=None, root_dirs=None, upgrade=False):
    """构造一个测试实例：根目录 + 自动分类子树 + 可选预置云端文件/文件夹。

    cloud_files：预置「散文件」（res_type=1），模拟单文件资源。
    cloud_dirs ：预置「文件夹」（res_type=2，磁力离线下载落盘的主要形态）。
    二者都用真实分类目录名（如「华语电影」）。
    root_files / root_dirs：预置到「转存根目录」，模拟旧版本/其它下载器
    （MoviePilot 等）平铺在根目录的资源（单文件 / 文件夹）。
    """
    cfg = AppConfig.load(str(BASE / "data" / "config.yaml"))
    if cfg_overrides:
        for k, v in cfg_overrides.items():
            setattr(cfg, k, v)
    client = FakeGuangya()
    # 转存根目录
    root = client.create_folder("", "TG转存")
    classifier = Classifier(mapping=cfg.organize.mapping or None,
                            structure=cfg.organize.structure, unknown_dir=cfg.organize.unknown_dir)
    resolver = CategoryResolver(client, root_id=root, create_missing=True)
    if cloud_files:
        for cat, fname in cloud_files:
            pid, _ = resolver.resolve(cat)
            client.dirs[pid]["files"][fname] = 1024
    if cloud_dirs:
        for cat, dirname in cloud_dirs:
            pid, _ = resolver.resolve(cat)
            client.create_folder(pid, dirname)
    if root_files:
        for fname in root_files:
            client.dirs[root]["files"][fname] = 1024
    if root_dirs:
        for dirname in root_dirs:
            client.create_folder(root, dirname)
    db = ":memory:"
    store = Store(db)
    dedup = CloudDedup(client, resolver, classifier,
                       cloud_check_new=cfg.dedup.cloud_check_new,
                       cache_ttl=cfg.dedup.cache_ttl,
                       organize_enabled=cfg.organize.enabled,
                       upgrade=upgrade)
    return client, resolver, classifier, store, dedup


def add_ledger(store, title, category="", quality=None):
    """模拟一次成功落盘：按身份识别把内容写进账本。"""
    info = ident_analyze(title)
    store.add_title(TitleRecord(
        norm_key=info.key,
        norm_core=ident_norm(info.core),
        sig=info.sig,
        is_pack=info.is_pack,
        year=info.year,
        title=title[:200],
        folder=info.folder,
        category=category or "",
        quality=quality if quality is not None else quality_score(title),
    ))
    return info


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

    # 4) 本地记录 done + 云端被删 → 云盘为准：盘里没有 → 重新落盘 retransfer
    #    （用户手动删/清理过盘里这份，重发同磁力应能再转回来）
    c, r, clf, st, dd = build()  # 云端无该文件
    st.add(MagnetRecord(hash="h_del", status="done", title="沙丘2 2024"))
    d = dd.decide("h_del", "沙丘2 Dune Part Two 2024 4K 国语中字", st)
    results.append(("本地done/云端已删→重落盘", "retransfer", d.action))

    # 4b) 本地 submitted（任务还在跑）+ 云端暂时还没有 → 继续等，不重复提交
    c, r, clf, st, dd = build()
    st.add(MagnetRecord(hash="h_pend", status="submitted", title="沙丘2 2024"))
    d = dd.decide("h_pend", "沙丘2 Dune Part Two 2024 4K 国语中字", st)
    results.append(("本地submitted/任务进行中", "skip_exists", d.action))

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
    cfg = AppConfig.load(str(BASE / "data" / "config.yaml"))
    cfg.organize.enabled = False
    c = FakeGuangya(); root = c.create_folder("", "TG转存"); c.dirs[root]["files"]["繁花.2023.1080p.mkv"] = 1
    clf = Classifier(); r2 = CategoryResolver(c, root_id=root, create_missing=False)
    st = Store(":memory:")
    dd2 = CloudDedup(c, r2, clf, cloud_check_new=True, organize_enabled=False)
    d = dd2.decide("h_fanhua", "繁花 更新至18集 1080P 国语中字", st)
    results.append(("未分类/根目录已有同名", "skip_exists", d.action))

    # 15) 【核心回归】云端已有同名「文件夹」（磁力落盘主要形态，res_type=2）
    #     不同磁力 hash 再来 → skip_exists（旧实现把文件夹全跳过 → 无限复制副本）
    c, r, clf, st, dd = build(cloud_dirs=[("华语电影", "流浪地球2.2023.1080p.BluRay")])
    d = dd.decide("hash_other_magnet2", "【电影】流浪地球2 The Wandering Earth II 2023 4K HDR 国语中字", st)
    results.append(("不同磁力/云端已有同名文件夹", "skip_exists", d.action))

    # 16) 【核心回归】文件夹名为中文（改名成功场景）：中文标题 → 命中文件夹
    c, r, clf, st, dd = build(cloud_dirs=[("华语电影", "黑夜告白.2026.2160p")])
    d = dd.decide("hash_heiye", "【电影】黑夜告白 2026 2160p 高清中字", st)
    results.append(("文件夹中文名命中", "skip_exists", d.action))

    # 17) 【拼音兜底】云端文件夹是拼音英文名 HeiYeGaoBai，频道标题全中文
    #     黑夜告白 → 旧机制中英核全落空 → 判定「云端没有」→ 副本；应命中跳过
    c, r, clf, st, dd = build(cloud_dirs=[("华语电影", "HeiYeGaoBai.2026.2160p.WEB-DL")])
    d = dd.decide("hash_heiye2", "【电影】黑夜告白 2026 2160p 高清中字", st)
    results.append(("文件夹拼音名命中(中文标题)", "skip_exists", d.action))

    # 18) 续集不误杀：云端《黑夜告白2》，新来《黑夜告白》→ transfer
    c, r, clf, st, dd = build(cloud_dirs=[("华语电影", "黑夜告白2.2026")])
    d = dd.decide("hash_heiye3", "【电影】黑夜告白 2026 2160p", st)
    results.append(("续集2不被前作误杀", "transfer", d.action))

    # 19) 【核心回归】MoviePilot/压制组长文件名平铺在「根目录」（本项目的分类目录下
    #     没有这条资源）：白夜追凶 整剧包目录带 {tv tmdb-73982}/[S01-S02]/体积尾巴，
    #     新来的同片磁力（中文标题）→ skip_exists
    #     （旧 title_core 剥不净这些尾巴 → 判「云端没有」→ 反复复制副本，文件夹/单文件皆然）
    c, r, clf, st, dd = build(root_dirs=[
        "白夜追凶 (2017){tv tmdb-73982}[S01-S02][2160p][HEVC][AAC][中字][2.0](67.7GB 61个文件)"])
    d = dd.decide("hash_byz", "【剧集】白夜追凶 全30集 高清中字", st)
    results.append(("根目录MoviePilot长名文件夹", "skip_exists", d.action))

    # 20) 单文件同样形态：MoviePilot 单文件带 tag，频道推中文标题 → skip_exists
    c, r, clf, st, dd = build(root_files=[
        "流浪地球2 (2023){movie tmdb-1983}[4K HDR][HEVC][中字].mkv"])
    d = dd.decide("hash_ld2v2", "【电影】流浪地球2 The Wandering Earth II 2023 4K 国语中字", st)
    results.append(("根目录MoviePilot长名单文件", "skip_exists", d.action))

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

    # 21) 【云端为准】同片不同磁力：云盘里已有该集 folder（规范名 庆余年.S01E06，
    #     此前落盘形成）→ 已存放弃 skip（账本有没有记录都一样，判据是云盘）
    c, r, clf, st, dd = build(cloud_dirs=[("国产剧", "庆余年.S01E06")])
    add_ledger(st, "庆余年 第06集 1080p 国语中字", "国产剧")
    d = dd.decide("hash_ep06_ledger", "庆余年 第6集 2160p 中字", st)
    results.append(("已存/同集不同磁力", "skip_exists", d.action))

    # 22) 【追剧核心】账本只有 第5集，频道新推 第6集（新磁力）→ transfer
    #     （集数签名进账本 key：第6集永远不会被第5集的记录误杀）
    c, r, clf, st, dd = build()
    add_ledger(st, "庆余年 第05集 1080p", "国产剧")
    d = dd.decide("hash_ep06_new", "庆余年 第06集 1080p 国语中字", st)
    results.append(("账本/第6集不被第5集误杀", "transfer", d.action))

    # 23) 【防误杀】云端已有整包目录「庆余年.2023」（无集号），新推 第6集 → transfer
    #     （旧逻辑会把「整包目录」当「已含第6集」→ 误杀；现在带集号资源不匹配无集号目录）
    c, r, clf, st, dd = build(cloud_dirs=[("国产剧", "庆余年.2023")])
    d = dd.decide("hash_ep06_packdir", "庆余年 第06集 1080p 国语中字", st)
    results.append(("云端整包目录不误杀新集", "transfer", d.action))

    # 24) 【追剧完整包】账本已有按集记录（第1~5集），频道再推「全30集」整包 → skip
    #     （逐集追过就别再被整包重复落盘；如整包含新增集可手动转存）
    c, r, clf, st, dd = build()
    for i in range(1, 6):
        add_ledger(st, f"庆余年 第0{i}集 1080p", "国产剧")
    d = dd.decide("hash_fullpack", "庆余年 全30集 高清中字", st)
    results.append(("整包/已按集收录则跳过", "skip_exists", d.action))

    # 25) 【云端为准】云盘已有《流浪地球2》规范 folder，另一频道再推同片不同磁力 → 已存放弃
    c, r, clf, st, dd = build(cloud_dirs=[("华语电影", "流浪地球2.2023")])
    add_ledger(st, "【电影】流浪地球2 The Wandering Earth II 2023 4K 国语中字", "华语电影")
    d = dd.decide("hash_ld2_other", "流浪地球2 2023 1080P 国语中字", st)
    results.append(("已存/电影同片不同磁力", "skip_exists", d.action))

    # 25b) 【云盘为准】账本有记录但云端 folder 已被删 → 没存 → 重新落盘 transfer
    c, r, clf, st, dd = build()
    add_ledger(st, "【电影】流浪地球2 The Wandering Earth II 2023 4K 国语中字", "华语电影")
    d = dd.decide("hash_ld2_gone", "流浪地球2 2023 1080P 国语中字", st)
    results.append(("账本有/云盘已删则重落盘", "transfer", d.action))

    # 26) 【洗版防误伤】账本已记录 1080P 旧版，再推同质量 1080P → 不触发洗版，直接 skip
    #     （新规范命名的云端文件夹名不含分辨率，旧质量只看账本记录的标题质量）
    c, r, clf, st, dd = build(upgrade=True, cloud_dirs=[("华语电影", "流浪地球2.2023")])
    add_ledger(st, "流浪地球2 2023 1080P 国语中字", "华语电影")
    d = dd.decide("hash_same_q", "流浪地球2 2023 1080p 国语中字", st)
    results.append(("洗版/账本同质量不再重复洗版", "skip_exists", d.action))

    # 27) 【洗版正向】账本旧版 1080P(60)，新推 2160P(120)，云端能找到旧 folder → upgrade 替换
    c, r, clf, st, dd = build(upgrade=True, cloud_dirs=[("华语电影", "流浪地球2.2023")])
    add_ledger(st, "流浪地球2 2023 1080P 国语中字", "华语电影")
    d = dd.decide("hash_4k", "流浪地球2 2023 4K 2160p 国语中字", st)
    results.append(("洗版/新质量更高则替换", "upgrade", d.action))
    if d.action == "upgrade":
        results.append(("洗版/携带待删旧文件id", "有", "有" if d.replace_file_id else "无"))

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
        # 带集号的新资源 vs 不带集号的云端目录：无法证明对端已含这一集（可能是整包/
        # 整季目录）→ 保守不判同片（宁首次重复多转一份，绝不漏集）。同集去重由账本兜底。
        ("鬼灭之刃 柱训练篇 第01集", "鬼灭之刃.柱训练篇.1080p.mkv", False),
        ("庆余年 第06集 1080p", "庆余年 第05集.mp4", False),       # 不同集 → 不误判为同片（追剧核心）
        ("庆余年 第05集 1080p", "庆余年 第05集.mp4", True),        # 同集同名 → 去重
        ("庆余年 S01E06", "庆余年 第06集.mp4", True),              # SxxExx 与 第X集 等价
        ("庆余年 第2季第3集", "庆余年 S02E03.mp4", True),          # 季+集 等价
        ("流浪地球2", "流浪地球 (2005).1080p.mkv", False),   # 续集 vs 前作
        ("沙丘", "沙丘2 2024 4K.mkv", False),                  # 沙丘 vs 沙丘2
        ("完全不同的片子", "另一部电影 2024.mkv", False),
        # 拼音兜底：中文标题 vs 拼音文件夹名
        ("黑夜告白 2026 2160p 高清中字", "HeiYeGaoBai.2026.2160p.WEB-DL", True),
        ("黑夜告白", "HeiYeGaoBai2.2026", False),              # 续集数字不误吞
        # MoviePilot/压制组自动下载器长文件名：{tmdb} 标签/[S01-S02]/体积尾巴应剥净
        ("白夜追凶 全30集 高清中字",
         "白夜追凶 (2017){tv tmdb-73982}[S01-S02][2160p][HEVC][AAC][中字][2.0](67.7GB 61个文件)", True),
        ("流浪地球2 4K 国语中字",
         "流浪地球2 (2023){movie tmdb-1983}[4K HDR][HEVC][中字].mkv", True),
        # 注：纯中文标题 vs 英文原名（星际穿越 vs Interstellar）依赖中英译名映射，
        # 文本层匹配不到，属于后续 TMDB 集成的范畴，这里不设用例。
    ]
    ok = 0
    for a, b, exp in cases:
        got = names_match(a, b)
        hit = "OK " if got == exp else "BAD"
        if got == exp: ok += 1
        print(f"  {hit} names_match({a!r}, {b!r}) = {got} (期望 {exp})")
    print(f"\n匹配单测: {ok}/{len(cases)} 通过")
    return ok == len(cases)


def test_ident():
    """身份识别确定性：同内容不同写法 → 同一 folder/key；不同集/续集 → 不同 key。"""
    from core.ident import analyze
    print("\n=== 标题身份识别（账本 key 来源）===")
    cases = [
        # (两个等价写法, 是否同一内容)
        ("庆余年 第06集 1080p 国语中字", "庆余年 第6集 2160p 中字", True),
        ("庆余年 第06集", "庆余年 S01E06", True),
        ("庆余年 第2季第3集 1080p", "庆余年 S02E03 2160p", True),
        ("【电影】流浪地球2 2023 4K 国语中字", "流浪地球2 2023 1080P 中英", True),
        ("黑夜告白 2026 2160p 高清中字", "黑夜告白 2026 WEB-DL", True),
        ("庆余年 第05集 1080p", "庆余年 第06集 1080p", False),     # 不同集 → 不同账本 key
        ("流浪地球 2023", "流浪地球2 2023", False),                 # 续集 → 不同 key
        ("金刚 2005 1080p", "金刚 2023 4K", False),               # 翻拍（年份不同）→ 不同 key
        ("狂飙 更新至第15集 1080p", "狂飙 更新至第18集 1080p", True),  # 追更包：都算《狂飙》整包
        ("狂飙 全30集 高清", "狂飙 1-30集 高清", True),             # 全集包写法不同 → 同 key
        ("白夜追凶 2017 全30集 高清中字",
         "白夜追凶 (2017){tv tmdb-73982}[S01-S02][2160p][HEVC][AAC][中字][2.0](67.7GB 61个文件)", True),
        # 中文标题夹带英文与否不影响 key（英文译名不稳定 → 中文为主时丢弃英文）
        ("奥本海默 Oppenheimer 2023 BluRay 英语中字", "奥本海默 2023 4K 国语", True),
    ]
    ok = 0
    for ta, tb, same in cases:
        a, b = analyze(ta), analyze(tb)
        got = (a.key == b.key)
        hit = "OK " if got == same else "BAD"
        if got == same: ok += 1
        print(f"  {hit} {ta[:30]!r:<34} key={a.folder!r:<24} | {tb[:26]!r:<30} key={b.folder!r:<24} 同一={same}")
    print(f"\n身份识别单测: {ok}/{len(cases)} 通过")
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
    i = test_ident()
    q = test_quality()
    print("\n==== 结论:", "全部通过 ✅" if (a and b and i and q) else "存在失败 ❌")
    sys.exit(0 if (a and b and i and q) else 1)
