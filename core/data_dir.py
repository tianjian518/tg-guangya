"""数据与存储卷解析。

容器部署时把宿主机目录挂到 /data，所有用户数据（配置、数据库、会话文件）
都放在那里，升级/重建容器都不会丢。本地直接跑则退回 ./data 或当前目录。

优先级：
    1. 环境变量 DATA_DIR（显式指定，最高优先）
    2. 容器挂载点 /data（存在即用）
    3. 当前目录已有 config.yaml（兼容旧版本地部署）
    4. 否则在当前目录新建 ./data
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_BASE = Path(__file__).resolve().parent.parent  # tg-guangya/


def _usable(p: Path) -> bool:
    """目录存在且可写。宿主机上若碰巧有个不可写的 /data（本机就是），
    必须识别出来并退回仓库目录，否则后面写文件会抛 PermissionError。"""
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(str(p), os.W_OK)


def get_data_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    if env:
        candidates = [Path(env), REPO_BASE / "data"]
    else:
        candidates = [
            Path("/data") if Path("/data").is_dir() else None,
            Path.cwd() if (Path.cwd() / "config.yaml").exists() else None,
            Path.cwd() / "data",
            REPO_BASE / "data",
        ]
    for c in candidates:
        if c is not None and _usable(c):
            return c
    # 全都不可写时兜底：至少让程序能启动，不至于在 import 阶段就崩
    return REPO_BASE / "data"


def resolve_config_path(name: str = "config.yaml") -> Path:
    """返回数据目录里的配置文件路径；不存在则先用仓库里的示例配置初始化。

    初始化失败（目录只读）不算致命错误——照常返回路径，
    由调用方在真正要写时再报错。这样 import main 也不会因为权限问题直接挂掉。
    """
    d = get_data_dir()
    cfg = d / name
    if not cfg.exists():
        example = REPO_BASE / "config.example.yaml"
        if example.exists():
            try:
                shutil.copyfile(example, cfg)
            except OSError as exc:  # 只读目录：忽略，交给调用方处理
                print(f"[warn] 无法用示例配置初始化 {cfg}: {exc}", file=sys.stderr)
    return cfg


def resolve_rel(data_dir: Path, p: str) -> str:
    """把配置里的相对路径解析到 data_dir；已是绝对路径则原样返回。"""
    if not p:
        return p
    pp = Path(p)
    if pp.is_absolute():
        return str(pp)
    return str(data_dir / pp)
