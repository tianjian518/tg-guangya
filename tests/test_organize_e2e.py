"""端到端验证「自动分类 + 自动建目录 + 转存到对应子目录」全链路。

用一个假的 GuangyaClient 模拟云盘（只在内存里维护目录树），
不需要真实光鸭账号即可验证：分类是否命中、目录是否自动建、磁力是否提交到正确目录。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.classifier import Classifier
from core.organizer import CategoryResolver
from core.store import Store
from core.matcher import KeywordFilter
from core.notifier import Notifier


class FakeMsg:
    def __init__(self, text, links, channel="testch", message_id="1"):
        self.text = text
        self.links = links
        self.channel = channel
        self.message_id = message_id


class FakeGuangya:
    """内存版光鸭：模拟目录树 + 离线任务提交。"""

    def __init__(self):
        self.dirs = {"": ("根目录", [])}      # id -> (name, [child ids])
        self.dir_map = {}                      # id -> (name, parent)
        self.next_id = 1
        self.submitted = []                    # (url, parent_id)

    def list_folders(self, parent_id=""):
        parent_id = parent_id or ""
        kids = self.dirs.get(parent_id, (None, []))[1]
        return [{"file_id": k, "name": self.dir_map[k][0], "parent_id": parent_id} for k in kids]

    def create_folder(self, parent_id="", name=""):
        parent_id = parent_id or ""
        fid = f"d{self.next_id}"
        self.next_id += 1
        self.dir_map[fid] = (name, parent_id)
        self.dirs.setdefault(parent_id, (None, []))[1].append(fid)
        self.dirs.setdefault(fid, (name, []))
        print(f"    [云盘] 新建目录: {self.path_of(fid)}")
        return fid

    def create_offline_task(self, url, parent_id=""):
        self.submitted.append((url, parent_id))
        return f"t{len(self.submitted)}", "name"

    def path_of(self, fid):
        parts = []
        cur = fid
        while cur and cur in self.dir_map:
            name, parent = self.dir_map[cur]
            parts.append(name)
            cur = parent
        return "/".join(reversed(parts)) or "根目录"


def main():
    client = FakeGuangya()
    root = client.create_folder("", "TG转存")          # 用户选的转存根目录
    store = Store(":memory:")
    flt = KeywordFilter()
    notifier = Notifier(console=False)
    clf = Classifier(structure="flat")
    resolver = CategoryResolver(client, root_id=root, create_missing=True)

    # 复用 main.py 的 handler 逻辑（这里简化复刻，验证分类→建目录→提交）
    def handle(msg):
        from core.store import MagnetRecord
        for url in msg.links:
            key = url[:40]
            if store.seen(key):
                continue
            store.add(MagnetRecord(hash=key, channel=msg.channel, title=msg.text[:80]))
            ok, reason = flt.match(msg.text)
            if not ok:
                store.update(key, status="skipped", reason=reason)
                continue
            cr = clf.classify(msg.text)
            target, path = resolver.resolve(cr.category)
            task_id, _ = client.create_offline_task(url, target)
            store.update(key, status="submitted", task_id=task_id, category=path)

    cases = [
        ("【电影】流浪地球2 The Wandering Earth II 2023 4K 国语中字", "华语电影"),
        ("奥本海默 Oppenheimer.2023.BluRay.1080p 英语中字", "欧美电影"),
        ("首尔之春 2023 韩语中字 1080P", "日韩电影"),
        ("繁花 更新至18集 1080P 国语中字", "国产剧"),
        ("权力的游戏 S01 1080p 英语中字", "欧美剧"),
        ("鬼灭之刃 柱训练篇 第01集 日语中字", "日本动漫"),
        ("歌手2024 第5期 1080P 国语", "综艺"),
        ("BBC 地球脉动 第三季 纪录片 1080P", "纪录片"),
        ("周杰伦 演唱会 2023 1080P 国语", "演唱会"),
        ("无间道 2002 粤语中字 1080P", "港台电影"),
    ]

    print("开始处理 10 条频道消息：\n")
    for i, (title, expect) in enumerate(cases, 1):
        print(f"  {i}. {title[:46]}")
        handle(FakeMsg(title, [f"magnet:?xt=urn:btih:case{i}"]))

    print("\n提交结果（磁力 → 实际落盘目录）：")
    ok = 0
    for (url, parent), (title, expect) in zip(client.submitted, cases):
        path = client.path_of(parent)
        hit = path.endswith(expect)
        ok += hit
        print(f"  {'OK ' if hit else 'BAD'} {path:<22} (期望 {expect})")

    print(f"\n目录实际创建情况：根目录下 {len(client.dirs[root][1])} 个子目录")
    for kid in client.dirs[root][1]:
        print(f"    - {client.dir_map[kid][0]}")

    print("\n数据库记录（去重库里的分类字段）：")
    for r in store.recent(limit=10)[::-1]:
        print(f"    {r.category:<10} {r.status:<10} {r.title[:34]}")

    print(f"\n=== 分类落盘准确率 {ok}/{len(cases)} ===")
    assert ok == len(cases), "存在分类落盘错误"
    assert len(client.dirs[root][1]) == len({e for _, e in cases}), "目录数应与分类数一致"
    print("全部断言通过")


if __name__ == "__main__":
    main()
