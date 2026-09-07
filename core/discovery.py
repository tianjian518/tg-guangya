"""自动发现 Telegram 影视频道。

用途：定期从"种子源"里抠出新的频道用户名，去重后自动加进配置，
让频道库越跑越全，不用你手动维护。

种子源可以是：
- 网络上的频道汇总页（GitHub 上的清单 raw 链接、盘搜聚合页等），
  只要页面文本里含 @username 或 t.me/xxx 即可；
- 本地种子文件（每行一个频道，见 config.discovery.seed_file）。

发现逻辑对存活不做保证（只收集+去重）。真正能否抓取，由主程序的
网页抓取器自然验证——抓不到的频道会被跳过、不会影响运行。

注意：发现源若包含 t.me 页面，需要运行环境能访问 t.me（部分网络受限，
发现到但抓不到也无害）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Iterable, Set

import requests

log = logging.getLogger(__name__)

# 匹配 @username 或 t.me/xxx ；频道用户名规则：5-32 位字母数字下划线
CHANNEL_RE = re.compile(r"(?:@|t\.me/|https?://t\.me/)([A-Za-z0-9_]{4,32})\b")
# 这些不是频道，需剔除
RESERVED = {
    "t.me", "telegram", "contact", "joinchat", "s", "addstickers",
    "verify", "botfather", "telegramorg", "webogram", "k", "share",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}[^\s\"'<>）】]*", re.I)


class ChannelDiscovery:
    MAX_CHANNELS = 50  # 自动发现上限，防止死频道撑爆轮询
    def __init__(
        self,
        seed_urls: Iterable[str] | None = None,
        seed_file: str = "",
        interval_hours: float = 24.0,
        timeout: int = 25,
        proxy: str = "",
        verify_threshold: int = 1,
    ) -> None:
        self.seed_urls = [str(u).strip() for u in (seed_urls or []) if u]
        self.seed_file = seed_file
        self.interval = max(1.0, float(interval_hours)) * 3600
        self.timeout = timeout
        self.proxy = proxy or None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        if self.proxy:
            self._session.proxies.update({
                "http": self.proxy,
                "https": self.proxy,
            })
        self.known: Set[str] = set()
        self._stop = threading.Event()
        # 新频道至少要有 N 条磁力才能被入库；0 = 不验证（全入库，兼容旧行为）
        self.verify_threshold = max(0, int(verify_threshold))

    def load_known(self, channels: Iterable[str]) -> None:
        self.known = {str(c).strip().lstrip("@").lower() for c in channels if c}

    def _load_seed_raw(self) -> Set[str]:
        """返回种子文件里的原始名字集合（不受上限限制）。"""
        names: Set[str] = set()
        if not self.seed_file:
            return names
        try:
            with open(self.seed_file, "r", encoding="utf-8") as f:
                for line in f:
                    names |= self.extract_names(line)
        except FileNotFoundError:
            log.warning("种子文件不存在: %s", self.seed_file)
        return names

    def load_seed_file(self) -> Set[str]:
        return self._load_seed_raw()

    @staticmethod
    def extract_names(text: str) -> Set[str]:
        out: Set[str] = set()
        for m in CHANNEL_RE.finditer(text or ""):
            name = m.group(1).lower()
            if name in RESERVED:
                continue
            out.add(name)
        return out

    def discover_once(self) -> Set[str]:
        found: Set[str] = set()
        for url in self.seed_urls:
            try:
                r = self._session.get(url, timeout=self.timeout)
                if r.status_code == 200:
                    found |= self.extract_names(r.text)
            except Exception as exc:
                log.warning("发现源抓取失败 %s: %s", url, exc)
        found |= self.load_seed_file()
        # 保留种子文件里的频道（不受上限限制），只限制自动发现的
        seed_names = {n for n in found if n in self._load_seed_raw()}
        existing_known = self.known & found
        auto_new = {n for n in found if n not in self.known and n not in seed_names}
        # 按字母序排序，取上限内的自动发现频道
        capped = sorted(auto_new)[:max(0, self.MAX_CHANNELS - len(existing_known))]
        new = seed_names | existing_known | set(capped)
        if len(capped) < len(auto_new):
            log.info("自动发现已截断：发现 %d 个，上限 %d 个（保留种子频道 %d 个）",
                     len(auto_new), self.MAX_CHANNELS, len(seed_names))
        return new

    def verify_channels(self, channels: Iterable[str], pages: int = 1,
                        delay: float = 0.8) -> Set[str]:
        """扫指定频道各 pages 页，只保留能产出 >= verify_threshold 条磁力的频道。

        返回经磁力验证后值得入库的频道集合。verify_threshold == 0 时跳过验证、全部通过。
        """
        if self.verify_threshold <= 0 or not channels:
            return set(channels)
        valid: Set[str] = set()
        skipped: list[str] = []
        for ch in sorted(channels):
            try:
                url = f"https://t.me/s/{ch}"
                r = self._session.get(url, timeout=self.timeout)
                if r.status_code != 200:
                    skipped.append(ch)
                    continue
                magnets = 0
                for _ in range(pages):
                    for m in MAGNET_RE.finditer(r.text):
                        magnets += 1
                        if magnets >= self.verify_threshold:
                            break
                    if magnets >= self.verify_threshold:
                        break
                if magnets >= self.verify_threshold:
                    valid.add(ch)
                else:
                    skipped.append(ch)
            except Exception as exc:
                log.debug("验证 %s 失败: %s", ch, exc)
                skipped.append(ch)
            time.sleep(delay)
        if skipped:
            log.info("磁力验证完成: %d 通过，%d 跳过（无磁力/不可达）: %s",
                     len(valid), len(skipped), ", ".join(skipped[:20]))
        else:
            log.info("磁力验证完成: %d 全部通过", len(valid))
        return valid

    def run(self, on_new: Callable[[Set[str]], None]) -> None:
        log.info(
            "频道自动发现已启动 | 种子源 %d 个 | 间隔 %.1fh | 磁力验证阈值=%d",
            len(self.seed_urls) + (1 if self.seed_file else 0),
            self.interval / 3600,
            self.verify_threshold,
        )
        while not self._stop.is_set():
            try:
                new = self.discover_once()
                if new:
                    log.info("发现 %d 个新频道: %s", len(new), ", ".join(sorted(new)[:30]))
                    verified = self.verify_channels(new)
                    self.known |= {n.lower() for n in new}
                    try:
                        on_new(verified)
                    except Exception as exc:
                        log.warning("处理新频道时出错: %s", exc)
            except Exception as exc:
                log.warning("发现循环异常: %s", exc)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
