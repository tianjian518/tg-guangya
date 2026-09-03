"""配置加载（YAML）。"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class GuangyaConfig:
    access_token: str = ""
    refresh_token: str = ""
    client_id: str = "aMe-8VSlkrbQXpUR"
    device_id: str = ""


@dataclass
class SourceConfig:
    type: str = "web"          # web | userbot
    channels: list[str] = field(default_factory=list)
    poll_interval: int = 120
    proxy: str = ""            # HTTP/HTTPS 代理地址，如 http://127.0.0.1:7890


@dataclass
class FilterConfig:
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    min_resolution: str = ""


@dataclass
class TelegramConfig:
    api_id: str = ""
    api_hash: str = ""
    session: str = "tg_user.session"


@dataclass
class OutputConfig:
    parent_id: str = ""        # 光鸭目标目录 fileId，留空为根
    save_path: str = ""        # 也可填目录名（需已存在）


@dataclass
class DiscoveryConfig:
    enabled: bool = False
    interval_hours: float = 24.0
    seed_urls: list[str] = field(default_factory=list)
    seed_file: str = ""        # 本地种子文件（每行一个 @用户名 或 t.me/xxx）


@dataclass
class OrganizeConfig:
    """自动分类转存：按内容形态 + 地区，自动建子目录并转存进去。"""
    enabled: bool = True
    structure: str = "flat"          # flat（华语电影）| two_level（电影/华语）
    create_missing: bool = True      # 分类目录不存在时自动创建
    unknown_dir: str = "未分类"       # 无法判定时的归属目录
    mapping: dict = field(default_factory=dict)  # {"movie:cn": "华语电影", ...}


@dataclass
class DedupConfig:
    """两级去重：本地记录 + 云端复查。

    - cloud_check_new: 首次出现（本地无记录）时，是否也去云端按片名复查，
      防止「同一片子不同磁力」被重复转存。默认开启，直接解决「重复七八上十次」。
    - cache_ttl: 云端目录列表缓存时长（秒），避免频道刷屏时对同一目录反复列举。
    - upgrade: 洗版/版本升级。盘里已有同名同集文件时，若新链接质量更优（更高分辨率、
      REMUX、Atmos 等）则删除旧版本、转存新版本；否则照常跳过。默认关闭，需显式开启
      （删除为不可逆操作，避免误删）。
    """
    cloud_check_new: bool = True
    cache_ttl: float = 300.0
    upgrade: bool = False


@dataclass
class AppConfig:
    guangya: GuangyaConfig = field(default_factory=GuangyaConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    organize: OrganizeConfig = field(default_factory=OrganizeConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    storage_db: str = "tg_guangya.db"
    notify_console: bool = True
    max_retries: int = 3
    scan_history: bool = False
    history_pages: int = 3

    @staticmethod
    def load(path: str) -> "AppConfig":
        if not os.path.exists(path):
            raise FileNotFoundError(f"配置文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
        cfg = AppConfig()
        g = raw.get("guangya") or {}
        cfg.guangya = GuangyaConfig(
            access_token=str(g.get("access_token", "")).strip(),
            refresh_token=str(g.get("refresh_token", "")).strip(),
            client_id=str(g.get("client_id", "aMe-8VSlkrbQXpUR")).strip(),
            device_id=str(g.get("device_id", "")).strip(),
        )
        s = raw.get("sources") or raw.get("source") or {}
        cfg.source = SourceConfig(
            type=str(s.get("type", "web")).strip().lower(),
            channels=[str(c).strip() for c in (s.get("channels") or []) if c],
            poll_interval=int(s.get("poll_interval", 120)),
            proxy=str(s.get("proxy", "")).strip(),
        )
        fl = raw.get("filter") or {}
        cfg.filter = FilterConfig(
            include_keywords=[str(x) for x in (fl.get("include_keywords") or [])],
            exclude_keywords=[str(x) for x in (fl.get("exclude_keywords") or [])],
            min_resolution=str(fl.get("min_resolution", "")).strip(),
        )
        tg = raw.get("telegram") or {}
        cfg.telegram = TelegramConfig(
            api_id=str(tg.get("api_id", "")).strip(),
            api_hash=str(tg.get("api_hash", "")).strip(),
            session=str(tg.get("session", "tg_user.session")).strip(),
        )
        o = raw.get("output") or {}
        cfg.output = OutputConfig(
            parent_id=str(o.get("parent_id", "")).strip(),
            save_path=str(o.get("save_path", "")).strip(),
        )
        cfg.storage_db = str(raw.get("storage_db", "tg_guangya.db")).strip()
        cfg.notify_console = bool(raw.get("notify_console", True))
        cfg.max_retries = int(raw.get("max_retries", 3))
        cfg.scan_history = bool(raw.get("scan_history", False))
        cfg.history_pages = int(raw.get("history_pages", 3))
        d = raw.get("discovery") or {}
        cfg.discovery = DiscoveryConfig(
            enabled=bool(d.get("enabled", False)),
            interval_hours=float(d.get("interval_hours", 24.0)),
            seed_urls=[str(x).strip() for x in (d.get("seed_urls") or []) if x],
            seed_file=str(d.get("seed_file", "")).strip(),
        )
        og = raw.get("organize") or {}
        cfg.organize = OrganizeConfig(
            enabled=bool(og.get("enabled", True)),
            structure=str(og.get("structure", "flat")).strip().lower(),
            create_missing=bool(og.get("create_missing", True)),
            unknown_dir=str(og.get("unknown_dir", "未分类")).strip() or "未分类",
            mapping={str(k).strip(): str(v).strip()
                     for k, v in (og.get("mapping") or {}).items() if k and v},
        )
        dd = raw.get("dedup") or {}
        cfg.dedup = DedupConfig(
            cloud_check_new=bool(dd.get("cloud_check_new", True)),
            cache_ttl=float(dd.get("cache_ttl", 300.0)),
            upgrade=bool(dd.get("upgrade", False)),
        )
        return cfg

    def add_channels(self, new_channels: list[str], path: str) -> int:
        """把新频道追加进配置（去重），返回实际新增数量。同时更新内存中的列表。"""
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        raw.setdefault("sources", {})
        existing = {str(c).strip().lstrip("@").lower() for c in (raw["sources"].get("channels") or [])}
        added = []
        for c in new_channels:
            name = str(c).strip().lstrip("@").lower()
            if name and name not in existing:
                existing.add(name)
                added.append(name)
        if not added:
            return 0
        raw["sources"]["channels"] = (raw["sources"].get("channels") or []) + added
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True)
        self.source.channels = [str(c).strip().lstrip("@") for c in raw["sources"]["channels"]]
        log.info("已追加 %d 个频道到 %s", len(added), path)
        return len(added)

    def save_token(self, access: str, refresh: str, path: str) -> None:
        """把新令牌写回配置文件（仅当文件存在时）。"""
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        raw.setdefault("guangya", {})
        raw["guangya"]["access_token"] = access
        raw["guangya"]["refresh_token"] = refresh
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True)
        # 同步内存
        self.guangya.access_token = access
        self.guangya.refresh_token = refresh
        log.info("令牌已写回 %s", path)

    def to_dict(self) -> dict:
        """导出为 YAML 兼容的字典。"""
        return {
            "guangya": {
                "access_token": self.guangya.access_token,
                "refresh_token": self.guangya.refresh_token,
                "client_id": self.guangya.client_id,
                "device_id": self.guangya.device_id,
            },
            "sources": {
                "type": self.source.type,
                "channels": list(self.source.channels),
                "poll_interval": self.source.poll_interval,
                "proxy": self.source.proxy,
            },
            "filter": {
                "include_keywords": list(self.filter.include_keywords),
                "exclude_keywords": list(self.filter.exclude_keywords),
                "min_resolution": self.filter.min_resolution,
            },
            "telegram": {
                "api_id": self.telegram.api_id,
                "api_hash": self.telegram.api_hash,
                "session": self.telegram.session,
            },
            "output": {
                "parent_id": self.output.parent_id,
                "save_path": self.output.save_path,
            },
            "discovery": {
                "enabled": self.discovery.enabled,
                "interval_hours": self.discovery.interval_hours,
                "seed_urls": list(self.discovery.seed_urls),
                "seed_file": self.discovery.seed_file,
            },
            "organize": {
                "enabled": self.organize.enabled,
                "structure": self.organize.structure,
                "create_missing": self.organize.create_missing,
                "unknown_dir": self.organize.unknown_dir,
                "mapping": dict(self.organize.mapping),
            },
            "dedup": {
                "cloud_check_new": self.dedup.cloud_check_new,
                "cache_ttl": self.dedup.cache_ttl,
                "upgrade": self.dedup.upgrade,
            },
            "storage_db": self.storage_db,
            "notify_console": self.notify_console,
            "max_retries": self.max_retries,
            "scan_history": self.scan_history,
            "history_pages": self.history_pages,
        }

    def save(self, path: str) -> None:
        """把整个配置写回 YAML（排序保持定义顺序）。"""
        with open(path, "w", encoding="utf-8") as f:
            # Python 3.7 的 PyYAML 3.13 不支持 sort_keys 参数
            yaml.safe_dump(self.to_dict(), f, allow_unicode=True)

    def apply_settings(self, s: dict) -> None:
        """合并前端提交的设置（不涉及令牌）。键缺失则保持不变。"""
        if not isinstance(s, dict):
            return
        src = s.get("sources") or {}
        if "type" in src:
            self.source.type = str(src["type"]).strip().lower()
        if "channels" in src:
            self.source.channels = [str(c).strip().lstrip("@") for c in src["channels"] if c]
        if "poll_interval" in src:
            self.source.poll_interval = max(30, int(src["poll_interval"]))
        fl = s.get("filter") or {}
        if "include_keywords" in fl:
            self.filter.include_keywords = [str(x).strip() for x in fl["include_keywords"] if x]
        if "exclude_keywords" in fl:
            self.filter.exclude_keywords = [str(x).strip() for x in fl["exclude_keywords"] if x]
        if "min_resolution" in fl:
            self.filter.min_resolution = str(fl["min_resolution"]).strip()
        out = s.get("output") or {}
        if "parent_id" in out:
            self.output.parent_id = str(out["parent_id"]).strip()
        if "save_path" in out:
            self.output.save_path = str(out["save_path"]).strip()
        d = s.get("discovery") or {}
        if "enabled" in d:
            self.discovery.enabled = bool(d["enabled"])
        if "interval_hours" in d:
            self.discovery.interval_hours = max(0.5, float(d["interval_hours"]))
        if "seed_urls" in d:
            self.discovery.seed_urls = [str(x).strip() for x in d["seed_urls"] if x]
        if "seed_file" in d:
            self.discovery.seed_file = str(d["seed_file"]).strip()
        og = s.get("organize") or {}
        if "enabled" in og:
            self.organize.enabled = bool(og["enabled"])
        if "structure" in og:
            v = str(og["structure"]).strip().lower()
            self.organize.structure = "two_level" if v == "two_level" else "flat"
        if "create_missing" in og:
            self.organize.create_missing = bool(og["create_missing"])
        if "unknown_dir" in og:
            self.organize.unknown_dir = str(og["unknown_dir"]).strip() or "未分类"
        if isinstance(og.get("mapping"), dict):
            self.organize.mapping = {
                str(k).strip(): str(v).strip()
                for k, v in og["mapping"].items() if k and v
            }
        dd = s.get("dedup") or {}
        if "cloud_check_new" in dd:
            self.dedup.cloud_check_new = bool(dd["cloud_check_new"])
        if "cache_ttl" in dd:
            self.dedup.cache_ttl = max(30.0, float(dd["cache_ttl"]))
        if "upgrade" in dd:
            self.dedup.upgrade = bool(dd["upgrade"])
        if "storage_db" in s:
            self.storage_db = str(s["storage_db"]).strip()
        if "notify_console" in s:
            self.notify_console = bool(s["notify_console"])
        if "max_retries" in s:
            self.max_retries = max(1, int(s["max_retries"]))
        if "scan_history" in s:
            self.scan_history = bool(s["scan_history"])
        if "history_pages" in s:
            self.history_pages = max(1, int(s["history_pages"]))
