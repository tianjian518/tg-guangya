# -*- coding: utf-8 -*-
"""清场：删转存根目录下全部条目 + 清离线任务记录。用法：python3 tools/cleanup_cloud.py [--dry]"""
import sys
sys.path.insert(0, ".")
from core.config import AppConfig
from core.guangya import GuangyaClient


def main() -> None:
    dry = "--dry" in sys.argv
    cfg = AppConfig.load("data/config.yaml")
    c = GuangyaClient(cfg.guangya.client_id, cfg.guangya.refresh_token)
    parent = cfg.output.parent_id
    items = c.list_dir(parent)
    print("根目录条目 %d 个" % len(items))
    if not dry:
        for it in items:
            try:
                c.delete_file(parent, it["file_id"])
                print("  删", it["name"])
            except Exception as exc:  # noqa: BLE001
                print("  失败", it["name"], exc)
        # 复查
        left = c.list_dir(parent)
        print("复查剩余:", len(left))
    # 离线任务记录清空
    if not dry:
        try:
            tasks = c.list_tasks()
            ids = [t.task_id for t in tasks]
            if ids:
                c.delete_tasks(ids)
            print("离线任务记录已清:", len(ids))
        except Exception as exc:  # noqa: BLE001
            print("任务清理异常:", exc)


if __name__ == "__main__":
    main()
