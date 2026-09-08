"""测试公共辅助：内存版光鸭、分类接线器、断言工具。

文件名以 _ 开头，避免被 pytest 当成测试模块收集。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.classifier import Classifier
from core.organizer import CategoryResolver
from core.ident import analyze as ident_analyze


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
        out = [{"file_id": sid, "name": self.dirs[sid]["name"], "size": 0,
                "res_type": 2, "md5": "", "parent_id": parent}
               for sid in d["subdirs"].values()]
        for fn, sz in d["files"].items():
            out.append({"file_id": "f_" + fn, "name": fn, "size": sz,
                        "res_type": 1, "md5": "", "parent_id": parent})
        return out

    def rename_file(self, file_id, new_name):
        for d in self.dirs.values():
            for sid in d["subdirs"]:
                if sid == file_id:
                    d["subdirs"][new_name] = d["subdirs"].pop(sid)
                    self.dirs[sid]["name"] = new_name
                    return True
        # 也可能是根目录下的文件
        for d in self.dirs.values():
            if file_id in d["files"]:
                d["files"][new_name] = d["files"].pop(file_id)
                return True
        return False


def build_resolver(structure: str = "flat", mapping=None):
    """构造 (client, classifier, resolver, root_id)，预置自动分类子树。"""
    client = FakeGuangya()
    root = client.create_folder("", "TG转存")
    classifier = Classifier(mapping=mapping, structure=structure, unknown_dir="未分类")
    resolver = CategoryResolver(client, root_id=root, create_missing=True)
    return client, classifier, resolver, root


def pick_category(title: str, classifier: Classifier, resolver: CategoryResolver):
    """复刻 main.make_handler.pick_target 的真实接线：ident → classify → resolve。"""
    info = ident_analyze(title)
    cr = classifier.classify(title, extra=info.folder,
                             region_hint=info.region_hint,
                             region_hint_strong=info.region_hint_strong,
                             kind_hint=info.kind_hint)
    target, path = resolver.resolve(cr.category)
    return {
        "category": cr.category,
        "path": path,
        "region_hint": info.region_hint,
        "region_hint_strong": info.region_hint_strong,
        "folder": info.folder,
        "target": target,
    }
