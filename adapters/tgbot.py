"""Telegram 机器人适配器（可选功能，默认关闭）。

和 userbot 完全是两套东西：

    userbot（adapters/userbot.py）  拿「你的账号」登录，用来实时监听频道
    机器人（本文件）                 拿「BotFather 的 token」，用来命令交互 + 推送通知

机器人能干的事（对着 Bot 直接说就行）：
    发一个磁力/迅雷/电驴链接      → 立即提交光鸭离线下载
    /status                      → 查看光鸭离线任务进度
    /stats                       → 查看转存统计（成功/失败/跳过各多少）
    /pause  /resume              → 暂停 / 恢复频道轮询
    /channels                    → 列出当前监听的频道
    /add 频道名  /del 频道名      → 增删频道（改的是配置文件，长期生效）
    /find 关键词                  → 在监控历史里搜资源
    /id                          → 查你自己的数字 ID（填 admin_ids 用）

依赖 pyTelegramBotAPI，没装时本模块不会让主程序崩——
未启用机器人时压根不会 import 它。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("tgbot")

HELP_TEXT = """🤖 *光鸭转存助手*

*直接发链接*：磁力 / 迅雷 / 电驴 / http 直链，我立刻提交离线下载。

*命令*
/status — 离线任务进度
/stats — 转存统计
/s `关键词` — 全网搜磁力（点按钮直接转存）
/saveall `关键词` — 搜到就整批转存
/pause — 暂停轮询
/resume — 恢复轮询
/channels — 当前监听的频道
/add `频道名` — 加频道
/del `频道名` — 删频道
/find `关键词` — 搜监控历史
/id — 查你的数字 ID
/help — 本帮助

提示：加了频道后新消息在下个轮询周期才会被抓到。"""


class BotMessage:
    """把用户在 TG 里发的链接包装成与频道消息同构的对象。

    这样机器人提交的链接能完整复用主流程的过滤、去重、自动分类、洗版逻辑，
    不需要另写一套——行为和频道里抓到的链接完全一致。
    """

    def __init__(self, links: List[str], text: str, channel: str = "tgbot",
                 message_id: str = "") -> None:
        self.links = links
        self.text = text
        self.channel = channel
        self.message_id = message_id


class TgBot:
    """Telegram 机器人：命令交互 + 结果推送。

    所有实际动作都通过构造时传入的回调完成，本类不碰光鸭、不碰数据库，
    方便单独测试，也避免和主循环抢资源。
    """

    def __init__(
        self,
        token: str,
        admin_ids: Optional[Iterable[int]] = None,
        proxy: str = "",
        allow_anyone: bool = False,
        on_submit: Optional[Callable[[List[str], str, int], str]] = None,
        on_status: Optional[Callable[[], str]] = None,
        on_stats: Optional[Callable[[], str]] = None,
        on_pause: Optional[Callable[[bool], str]] = None,
        on_channels: Optional[Callable[[], List[str]]] = None,
        on_add_channel: Optional[Callable[[str], str]] = None,
        on_del_channel: Optional[Callable[[str], str]] = None,
        on_find: Optional[Callable[[str], str]] = None,
        on_search: Optional[Callable[[str, int], Tuple[List[Dict[str, Any]], str]]] = None,
    ) -> None:
        self.token = (token or "").strip()
        self.admin_ids = {int(i) for i in (admin_ids or []) if int(i)}
        self.proxy = (proxy or "").strip()
        self.allow_anyone = bool(allow_anyone)

        self._on_submit = on_submit
        self._on_status = on_status
        self._on_stats = on_stats
        self._on_pause = on_pause
        self._on_channels = on_channels
        self._on_add_channel = on_add_channel
        self._on_del_channel = on_del_channel
        self._on_find = on_find
        self._on_search = on_search

        self._bot = None          # telebot.TeleBot 实例（start 后才有）
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._paused = False
        self._admins_cache_lock = threading.Lock()
        self._last_notify = 0.0   # 推送节流时间戳

        # /s 搜索结果缓存：slot id -> 结果 dict（callback 按钮点回来时取用）
        self._search_slots: Dict[str, Dict[str, Any]] = {}
        self._search_seq = 0
        self._search_lock = threading.Lock()

    # ------------------------------------------------------------------ 权限

    def allowed(self, user_id: int) -> bool:
        """是否放行该用户。没配 admin_ids 且不放开所有人时，只放行已缓存的管理员。"""
        if self.allow_anyone:
            return True
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return False
        return uid in self.admin_ids

    def _deny(self, chat_id: int) -> None:
        self._send(chat_id, "⛔ 你不在管理员名单里。用 /id 查到自己的 ID 后，"
                            "填进配置的 bot.admin_ids 再重启即可。")

    # ------------------------------------------------------------------ 启动

    def start(self) -> bool:
        """初始化并启动轮询（阻塞式，内部自带重连）。失败返回 False。"""
        if not self.token:
            log.warning("TG 机器人未配置 token，跳过启动")
            return False
        try:
            import telebot  # 延迟导入：没装依赖也不影响主程序
            from telebot import apihelper
        except ImportError:
            log.warning("未安装 pyTelegramBotAPI，机器人功能不可用（pip install pyTelegramBotAPI）")
            return False

        if self.proxy:
            apihelper.proxy = {"http": self.proxy, "https": self.proxy}
            log.info("TG 机器人走代理: %s", self.proxy)

        self._bot = telebot.TeleBot(self.token, parse_mode="Markdown")
        self._register(telebot)
        log.info("TG 机器人已启动（管理员 %d 位）", len(self.admin_ids))

        # 轮询自带重连：网络抖动时不会整个程序退出
        while not self._stop.is_set():
            try:
                self._bot.infinity_polling(timeout=30, long_polling_timeout=20,
                                           logger_level=logging.WARNING)
            except Exception as exc:  # noqa: BLE001 - 轮询异常一律重连
                if self._stop.is_set():
                    break
                log.warning("TG 机器人轮询异常，10 秒后重连: %s", exc)
                time.sleep(10)
        return True

    def start_thread(self, name: str = "tgbot") -> Optional[threading.Thread]:
        """在后台线程里跑机器人，不阻塞主循环。"""
        if not self.token:
            return None
        self._thread = threading.Thread(target=self.start, name=name, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._bot is not None:
                self._bot.stop_polling()
        except Exception:  # noqa: BLE001 - 停止失败无需处理
            pass

    # ------------------------------------------------------------ 全网磁力搜索

    def _run_search(self, chat_id: int, kw: str, auto_all: bool) -> None:
        """在后台线程执行搜索：/s 出列表+按钮；/saveall 直接整批提交。"""
        try:
            hits, errors = self._on_search(kw, 8)
        except Exception as exc:  # noqa: BLE001
            log.warning("全网搜索异常: %s", exc)
            self._send(chat_id, f"❌ 搜索出错了：{exc}")
            return
        hits = hits or []
        if not hits:
            tip = "换个说法试试？中文片建议用英文名或原名（`/s interstellar`）。"
            if errors:
                tip += f"\n引擎报错：{'；'.join(errors)[:120]}"
            self._send(chat_id, f"没找到「{kw}」的磁力。\n{tip}")
            return

        if auto_all:
            # 整批转存前 6 个（每个都走同一条过滤/去重/转存流水线）
            ok = 0
            for h in hits[:6]:
                try:
                    self._on_submit(f"{h.get('title', '')}\n{h.get('magnet', '')}", chat_id)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("批量转存单条失败: %s", exc)
            self._send(chat_id, f"🚚 已提交 {ok} 条离线下载（结果稍后逐条推送）。")
            return
        self._render_search_result(chat_id, kw, hits)

    def _render_search_result(self, chat_id: int, kw: str, hits: List[Dict[str, Any]]) -> None:
        try:
            from telebot import types
        except ImportError:
            self._send(chat_id, "机器人组件缺失，无法显示按钮。")
            return
        shown = hits[:6]
        lines = [f"🔎 「{kw}」磁力结果（按做种人数）"]
        mark = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣")
        for i, h in enumerate(shown):
            t = h.get("title") or "未命名"
            size = h.get("size_text") or "-"
            seeds = h.get("seeders") or 0
            lines.append(f"{mark[i]} {t[:90]}\n    大小 {size} · 做种 {seeds}")
        with self._search_lock:
            rows: List[Any] = []
            for i in range(len(shown)):
                self._search_seq += 1
                slot = f"s{self._search_seq}"
                self._search_slots[slot] = shown[i]
                rows.append(types.InlineKeyboardButton(
                    f"⬇️ 存{i + 1}", callback_data=f"g:{slot}"))
            # 每行放 3 个按钮，最多 2 行
            keyboard = [rows[i:i + 3] for i in range(0, len(rows), 3)]
        markup = types.InlineKeyboardMarkup(keyboard)
        lines.append("点【存N】即转存；全部搜到整批转存用 /saveall。")
        if self._bot is None:
            return
        try:
            self._bot.send_message(chat_id, "\n".join(lines),
                                   parse_mode=None, disable_web_page_preview=True,
                                   reply_markup=markup)
        except Exception as exc:  # noqa: BLE001
            log.warning("发送搜索结果失败: %s", exc)

    def _handle_callback(self, c) -> None:
        """行内按钮回调：把对应磁力提交离线下载。"""
        try:
            uid = (c.from_user.id if c.from_user else 0) or 0
            if not self.allowed(uid):
                self._safe_answer(c, "⛔ 无权限")
                return
            data = c.data or ""
            if not data.startswith("g:"):
                self._safe_answer(c, "未知操作")
                return
            with self._search_lock:
                hit = self._search_slots.pop(data[2:], None)
            if hit is None:
                self._safe_answer(c, "结果已过期，重新 /s 一下")
                return
            self._safe_answer(c, "收到，开始提交…")
            text = f"{hit.get('title', '')}\n{hit.get('magnet', '')}"
            if self._on_submit is not None:
                reply = self._on_submit(text, c.message.chat.id)
                self._send(c.message.chat.id, reply)
        except Exception as exc:  # noqa: BLE001 - 回调异常不能拖垮轮询
            log.warning("回调处理异常: %s", exc)

    def _safe_answer(self, c, text: str) -> None:
        if self._bot is None:
            return
        try:
            self._bot.answer_callback_query(c.id, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("应答回调失败: %s", exc)

    # ------------------------------------------------------------------ 发送

    def _send(self, chat_id: int, text: str) -> None:
        if self._bot is None:
            return
        try:
            # 超长消息分段发，避免超过 TG 4096 字符上限
            for i in range(0, len(text), 3500):
                self._bot.send_message(chat_id, text[i:i + 3500],
                                       disable_web_page_preview=True)
        except Exception as exc:  # noqa: BLE001 - 单条发送失败不应影响主流程
            log.warning("TG 发送失败: %s", exc)

    def notify(self, text: str) -> None:
        """把转存结果推送给所有管理员（供 Notifier 回调使用）。"""
        if not self.admin_ids or self._bot is None:
            return
        # 简单节流：同一秒内的多条通知合并丢弃，避免刷屏
        now = time.time()
        with self._admins_cache_lock:
            if now - self._last_notify < 1.0:
                self._last_notify = now
                return
            self._last_notify = now
        for uid in self.admin_ids:
            self._send(uid, text)

    # ------------------------------------------------------------------ 命令

    def _register(self, telebot_mod) -> None:
        bot = self._bot

        @bot.message_handler(commands=["start", "help"])
        def _help(message):
            self._send(message.chat.id, HELP_TEXT)

        @bot.message_handler(commands=["id"])
        def _id(message):
            uid = message.from_user.id if message.from_user else 0
            self._send(message.chat.id, f"你的数字 ID 是：`{uid}`\n"
                                        f"把它填进配置的 `bot.admin_ids` 就能用全部命令。")

        @bot.message_handler(commands=["status"])
        def _status(message):
            if not self.allowed(message.from_user.id if message.from_user else 0):
                return self._deny(message.chat.id)
            self._send(message.chat.id, self._call(self._on_status, "暂不支持"))

        @bot.message_handler(commands=["stats"])
        def _stats(message):
            if not self.allowed(message.from_user.id if message.from_user else 0):
                return self._deny(message.chat.id)
            self._send(message.chat.id, self._call(self._on_stats, "暂不支持"))

        @bot.message_handler(commands=["pause", "resume"])
        def _pause(message):
            if not self.allowed(message.from_user.id if message.from_user else 0):
                return self._deny(message.chat.id)
            want_pause = message.text.strip().lower().startswith("/pause")
            if self._on_pause is None:
                self._send(message.chat.id, "当前运行模式下不支持暂停/恢复")
                return
            self._paused = want_pause
            self._send(message.chat.id, self._on_pause(want_pause))

        @bot.message_handler(commands=["channels"])
        def _channels(message):
            if not self.allowed(message.from_user.id if message.from_user else 0):
                return self._deny(message.chat.id)
            chs = self._on_channels() if self._on_channels else []
            if not chs:
                self._send(message.chat.id, "当前没有监听任何频道。")
                return
            body = "\n".join(f"• `{c}`" for c in chs[:80])
            more = f"\n\n（共 {len(chs)} 个，仅显示前 80）" if len(chs) > 80 else ""
            self._send(message.chat.id, f"📺 *监听中的频道*\n\n{body}{more}")

        @bot.message_handler(commands=["add", "del"])
        def _ch_edit(message):
            uid = message.from_user.id if message.from_user else 0
            if not self.allowed(uid):
                return self._deny(message.chat.id)
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                self._send(message.chat.id, "用法：`/add 频道名` 或 `/del 频道名`")
                return
            name = parts[1].strip().lstrip("@").split("/")[-1].strip()
            is_del = message.text.strip().lower().startswith("/del")
            fn = self._on_del_channel if is_del else self._on_add_channel
            if fn is None:
                self._send(message.chat.id, "当前运行模式下不支持增删频道")
                return
            self._send(message.chat.id, fn(name))

        @bot.message_handler(commands=["find"])
        def _find(message):
            if not self.allowed(message.from_user.id if message.from_user else 0):
                return self._deny(message.chat.id)
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                self._send(message.chat.id, "用法：`/find 关键词`")
                return
            if self._on_find is None:
                self._send(message.chat.id, "暂不支持搜索")
                return
            self._send(message.chat.id, self._on_find(parts[1].strip()))

        @bot.message_handler(commands=["s", "search", "saveall"])
        def _search_cmd(message):
            if not self.allowed(message.from_user.id if message.from_user else 0):
                return self._deny(message.chat.id)
            if self._on_search is None:
                self._send(message.chat.id, "未启用全网磁力搜索（配置里把 bot.search_enabled 打开并重启监听）")
                return
            parts = (message.text or "").split(maxsplit=1)
            kw = (parts[1] if len(parts) > 1 else "").strip()
            if not kw:
                self._send(message.chat.id, "用法：`/s 关键词`\n搜磁力引擎的种子名，"
                                            "中文片建议用英文名/原名（如 `/s interstellar`）。\n"
                                            "想搜到就整批转存用 `/saveall 关键词`。")
                return
            auto = message.text.strip().lower().startswith("/saveall")
            self._send(message.chat.id, ("🚚 正在全网搜「%s」并整批转存…" if auto
                                         else "🔍 正在全网搜「%s」…（几秒）") % kw)
            threading.Thread(target=self._run_search, args=(message.chat.id, kw, auto),
                             daemon=True, name="bot-search").start()

        @bot.callback_query_handler(func=lambda c: True)
        def _on_callback(c):
            self._handle_callback(c)

        @bot.message_handler(func=lambda m: True, content_types=["text"])
        def _text(message):
            uid = message.from_user.id if message.from_user else 0
            if not self.allowed(uid):
                return self._deny(message.chat.id)
            if self._on_submit is None:
                self._send(message.chat.id, "提交功能未就绪")
                return
            self._send(message.chat.id, self._on_submit(message.text or "", message.chat.id))

    @staticmethod
    def _call(fn: Optional[Callable[[], str]], fallback: str) -> str:
        if fn is None:
            return fallback
        try:
            return fn() or fallback
        except Exception as exc:  # noqa: BLE001 - 回调异常转成文本回给用户
            return f"查询失败：{exc}"
