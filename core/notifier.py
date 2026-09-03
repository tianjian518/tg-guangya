"""轻量通知模块。

默认用控制台输出；可叠加回调（例如接到 TG / 企业微信 / 邮件）。
不引入额外依赖，方便在内网、NAS 上直接跑。
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, console: bool = True) -> None:
        self.console = console
        self._callbacks: list[Callable[[str], None]] = []

    def on_message(self, cb: Callable[[str], None]) -> None:
        self._callbacks.append(cb)

    def send(self, text: str) -> None:
        if self.console:
            log.info("[通知] %s", text)
        for cb in self._callbacks:
            try:
                cb(text)
            except Exception as exc:  # 单个通知渠道失败不应中断主流程
                log.warning("通知回调失败: %s", exc)
