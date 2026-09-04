"""TG 机器人功能的离线自测（不需要真实 Telegram 网络，不需要光鸭账号）。

覆盖：
  1. bot 配置段的解析 / 回写 / 前端设置合并
  2. 管理员权限判定
  3. main.start_bot 里各命令回调的逻辑（提交、查状态、统计、暂停、增删频道、搜索）
  4. 机器人提交的链接能完整走通主流程（过滤 → 去重 → 分类 → 提交）

跑法：
    python -m pytest tests/test_bot_offline.py -v
    # 或
    python tests/test_bot_offline.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from adapters.tgbot import TgBot, BotMessage  # noqa: E402
from adapters.web_scraper import WebScraper, extract_links  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.store import Store, MagnetRecord  # noqa: E402


def write_cfg(d: str, data: dict) -> str:
    p = os.path.join(d, "config.yaml")
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)
    return p


class FakeTask:
    def __init__(self, name, status, progress=0, size=0, message="", task_id="t1"):
        self.task_id = task_id
        self.file_id = ""
        self.name = name
        self.size = size
        self.status = status
        self.progress = progress
        self.message = message

    @property
    def finished(self):
        return self.status in (2, 3)

    @property
    def ok(self):
        return self.status == 2


class TestBotConfig(unittest.TestCase):
    """bot 配置段能正确读进内存、再原样写回文件。"""

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_cfg(d, {
                "guangya": {"access_token": "a", "refresh_token": "r"},
                "sources": {"channels": ["ysh365"], "proxy": "http://127.0.0.1:7890"},
                "bot": {"enabled": True, "token": "123:ABC", "admin_ids": [42, 43],
                        "notify": False, "proxy": "", "allow_anyone": False},
            })
            cfg = AppConfig.load(p)
            self.assertTrue(cfg.bot.enabled)
            self.assertEqual(cfg.bot.token, "123:ABC")
            self.assertEqual(cfg.bot.admin_ids, [42, 43])
            self.assertFalse(cfg.bot.notify)
            self.assertEqual(cfg.bot.proxy, "")  # 空则为空，不继承 sources.proxy

            out = os.path.join(d, "out.yaml")
            cfg.save(out)
            with open(out, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            self.assertEqual(raw["bot"]["admin_ids"], [42, 43])
            self.assertTrue(raw["bot"]["enabled"])
            self.assertEqual(raw["sources"]["channels"], ["ysh365"])

    def test_missing_section_defaults(self):
        """老配置文件里没有 bot 段时不应报错，全部走默认值。"""
        with tempfile.TemporaryDirectory() as d:
            p = write_cfg(d, {"sources": {"channels": ["abc"]}})
            cfg = AppConfig.load(p)
            self.assertFalse(cfg.bot.enabled)
            self.assertEqual(cfg.bot.admin_ids, [])

    def test_apply_settings(self):
        cfg = AppConfig()
        cfg.apply_settings({"bot": {"enabled": True, "token": "9:ZZZ",
                                    "admin_ids": ["7", "8", "x"], "notify": False}})
        self.assertTrue(cfg.bot.enabled)
        self.assertEqual(cfg.bot.token, "9:ZZZ")
        self.assertEqual(cfg.bot.admin_ids, [7, 8])   # 非数字被丢弃
        self.assertFalse(cfg.bot.notify)


class TestPermission(unittest.TestCase):
    def test_only_admin(self):
        bot = TgBot(token="x", admin_ids=[1, 2])
        self.assertTrue(bot.allowed(1))
        self.assertTrue(bot.allowed(2))
        self.assertFalse(bot.allowed(999))
        self.assertFalse(bot.allowed(None))

    def test_allow_anyone(self):
        bot = TgBot(token="x", admin_ids=[1], allow_anyone=True)
        self.assertTrue(bot.allowed(999))

    def test_no_admin_configured(self):
        """没配 admin_ids 时谁都不能用（避免机器人被陌生人滥用）。"""
        bot = TgBot(token="x")
        self.assertFalse(bot.allowed(1))


class TestBotCallbacks(unittest.TestCase):
    """把 main.start_bot 里的回调抠出来单独测：用一个假 TgBot 接住它们。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = write_cfg(self.tmp.name, {
            "guangya": {"access_token": "a", "refresh_token": "r"},
            "sources": {"channels": ["ysh365", "seedhub_cc"], "proxy": ""},
            "bot": {"enabled": True, "token": "1:A", "admin_ids": [7]},
        })
        self.cfg = AppConfig.load(self.cfg_path)
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.store.add(MagnetRecord(hash="h1", title="权力的游戏 S01 1080p",
                                    status="done", category="欧美剧"))
        self.store.add(MagnetRecord(hash="h2", title="某电影 4K",
                                    status="failed", reason="配额不足"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _capture(self):
        """调 start_bot 但把 TgBot 换成假的，返回它收到的回调字典。"""
        import main as main_mod

        captured = {}

        class FakeBot:
            def __init__(self, **kw):
                captured.update(kw)

            def start_thread(self, *a, **k):
                return None

            def notify(self, text):
                captured.setdefault("_notified", []).append(text)

        scraper = WebScraper(["ysh365", "seedhub_cc"])
        with mock.patch.object(main_mod, "TgBot", FakeBot):
            main_mod.start_bot(
                self.cfg, self.cfg_path, self.store,
                mock.MagicMock(),          # client
                mock.MagicMock(),          # handler
                scraper,
                mock.MagicMock(),          # notifier
            )
        captured["_scraper"] = scraper
        return captured

    def test_submit_rejects_plain_text(self):
        cbs = self._capture()
        out = cbs["on_submit"]("今天天气不错", 700)
        self.assertIn("没识别到", out)

    def test_submit_accepts_magnet(self):
        cbs = self._capture()
        magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Test.Movie.2026.1080p"
        out = cbs["on_submit"](magnet, 700)
        self.assertIn("已开始提交", out)
        self.assertIn("1 个链接", out)

    def test_pause_and_resume(self):
        cbs = self._capture()
        scraper = cbs["_scraper"]
        self.assertIn("已暂停", cbs["on_pause"](True))
        self.assertTrue(scraper.pause_event.is_set())
        self.assertIn("已恢复", cbs["on_pause"](False))
        self.assertFalse(scraper.pause_event.is_set())

    def test_pause_unsupported_in_userbot(self):
        import main as main_mod

        captured = {}

        class FakeBot:
            def __init__(self, **kw):
                captured.update(kw)

            def start_thread(self, *a, **k):
                return None

            def notify(self, text):
                captured.setdefault("_notified", []).append(text)

        with mock.patch.object(main_mod, "TgBot", FakeBot):
            main_mod.start_bot(self.cfg, self.cfg_path, self.store,
                               mock.MagicMock(), mock.MagicMock(),
                               None,  # 没有 scraper = userbot 模式
                               mock.MagicMock())
        self.assertIn("不支持", captured["on_pause"](True))

    def test_add_and_del_channel(self):
        cbs = self._capture()
        self.assertIn("已添加", cbs["on_add_channel"]("newmovie"))
        self.assertIn("newmovie", cbs["on_channels"]())
        self.assertIn("已在列表里", cbs["on_add_channel"]("newmovie"))

        self.assertIn("已删除", cbs["on_del_channel"]("newmovie"))
        self.assertNotIn("newmovie", cbs["on_channels"]())
        self.assertIn("不在列表里", cbs["on_del_channel"]("newmovie"))

        # 删除必须写回配置文件，重启后依然生效
        raw = yaml.safe_load(open(self.cfg_path, encoding="utf-8"))
        self.assertNotIn("newmovie", raw["sources"]["channels"])

    def test_add_syncs_running_scraper(self):
        """加了频道后，正在跑的抓取器要立刻感知，否则得重启才生效。"""
        cbs = self._capture()
        scraper = cbs["_scraper"]
        cbs["on_add_channel"]("tvbox888")
        self.assertIn("tvbox888", scraper.channels)

    def test_find(self):
        cbs = self._capture()
        out = cbs["on_find"]("权力")
        self.assertIn("权力", out)
        self.assertIn("欧美剧", out)

        self.assertIn("没找到", cbs["on_find"]("不存在的片子xyz"))

    def test_stats(self):
        cbs = self._capture()
        out = cbs["on_stats"]()
        self.assertIn("2", out)
        self.assertIn("done", out)

    def test_status_with_tasks(self):
        import main as main_mod

        captured = {}

        class FakeBot:
            def __init__(self, **kw):
                captured.update(kw)

            def start_thread(self, *a, **k):
                return None

            def notify(self, text):
                captured.setdefault("_notified", []).append(text)

        client = mock.MagicMock()
        client.list_tasks.return_value = [
            FakeTask("Movie.A.2026", 1, progress=45, size=2 * 1024 ** 3),
            FakeTask("Movie.B.2026", 2, progress=100),
        ]
        scraper = WebScraper(["ysh365"])
        with mock.patch.object(main_mod, "TgBot", FakeBot):
            main_mod.start_bot(self.cfg, self.cfg_path, self.store,
                               client, mock.MagicMock(), scraper, mock.MagicMock())
        out = captured["on_status"]()
        self.assertIn("进行中 1", out)
        self.assertIn("完成 1", out)
        self.assertIn("45%", out)
        self.assertIn("GB", out)   # 大小已人类可读化

    def test_status_when_guangya_error(self):
        import main as main_mod
        from core.guangya import GuangyaError

        captured = {}

        class FakeBot:
            def __init__(self, **kw):
                captured.update(kw)

            def start_thread(self, *a, **k):
                return None

            def notify(self, text):
                captured.setdefault("_notified", []).append(text)

        client = mock.MagicMock()
        client.list_tasks.side_effect = GuangyaError("令牌失效")
        with mock.patch.object(main_mod, "TgBot", FakeBot):
            main_mod.start_bot(self.cfg, self.cfg_path, self.store,
                               client, mock.MagicMock(), WebScraper(["a"]),
                               mock.MagicMock())
        self.assertIn("查询失败", captured["on_status"]())


class TestBotMessageFlow(unittest.TestCase):
    """机器人提交的链接必须和频道抓到的一样，走完整的过滤/去重/分类流程。"""

    def test_botmessage_shape(self):
        links = extract_links("magnet:?xt=urn:btih:" + "b" * 40 + "&dn=X")
        m = BotMessage(links=links, text="测试资源", message_id="700")
        self.assertEqual(len(links), 1)
        self.assertEqual(m.channel, "tgbot")
        self.assertEqual(m.text, "测试资源")

    def test_handler_consumes_botmessage(self):
        """主流程 handler 能直接吃 BotMessage（含过滤分支）。"""
        from core.matcher import KeywordFilter
        from core.notifier import Notifier
        import main as main_mod

        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "t.db"))
            client = mock.MagicMock()
            client.create_offline_task.return_value = ("task-1", "Name")
            client.wait_offline_task.return_value = (2, "完成")

            flt = KeywordFilter(["电影"], [], "")
            notifier = Notifier(console=False)

            handler = main_mod.make_handler(
                store, client, flt, notifier, "root", 1,
                classifier=None, resolver=None, dedup=None, organize_enabled=False,
            )
            magnet = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=Movie"
            m = BotMessage(links=[magnet], text="某电影 1080p", message_id="1")
            handler(m)

            rec = store.get(main_mod.link_key(magnet))
            self.assertIsNotNone(rec, "机器人提交的链接应当入库")
            self.assertEqual(rec.status, "done")
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
