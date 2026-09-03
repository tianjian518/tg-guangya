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
from pathlib import Path

REPO_BASE = Path(__file__).resolve().parent.parent  # tg-guangya/


def get_data_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    if env:
        p = Path(env)
    elif Path("/data").is_dir():
        p = Path("/data")
    elif (Path.cwd() / "config.yaml").exists():
        p = Path.cwd()
    else:
        p = Path.cwd() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_config_path(name: str = "config.yaml") -> Path:
    """返回数据目录里的配置文件路径；不存在则先用仓库里的示例配置初始化。"""
    d = get_data_dir()
    cfg = d / name
    if not cfg.exists():
        example = REPO_BASE / "config.example.yaml"
        if example.exists():
            shutil.copyfile(example, cfg)
    return cfg


def resolve_rel(data_dir: Path, p: str) -> str:
    """把配置里的相对路径解析到 data_dir；已是绝对路径则原样返回。"""
    if not p:
        return p
    pp = Path(p)
    if pp.is_absolute():
        return str(pp)
    return str(data_dir / pp)
