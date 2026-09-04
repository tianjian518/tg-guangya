"""TG 频道磁力/迅雷/电驴 → 光鸭云盘（离线下载）自动转存——主程序。

用法:
    # 首次运行：扫码登录光鸭，生成配置
    python login.py            # 扫码，把令牌写进 config.yaml
    # 日常运行
    python main.py --config config.yaml

来源切换（config.yaml 的 sources.type）：
    web     公开频道网页抓取，无需登录，零风控（推荐）
    userbot 用你账号实时监听，需 telethon + api_id/api_hash（建议小号）

自动发现频道：config.yaml 的 discovery.enabled=true 后，主程序会起一个后台线程，
定期从 seed_urls / seed_file 里抠出新的影视频道，自动追加进配置。
"""
from __future__ import annotations

import argparse
import logging
import re
import threading
import time

from adapters.web_scraper import WebScraper, link_key, extract_links
from adapters.userbot import UserbotSource
from adapters.tgbot import TgBot, BotMessage
from core.guangya import GuangyaClient, GuangyaError, STATUS_TEXT
from core.store import Store, MagnetRecord, TitleRecord
from core.matcher import KeywordFilter, parse_title
from core.naming import build_cn_filename
from core.notifier import Notifier
from core.config import AppConfig
from core.discovery import ChannelDiscovery
from core.data_dir import resolve_config_path, get_data_dir, resolve_rel
from core.classifier import Classifier
from core.organizer import CategoryResolver
from core.dedup import CloudDedup, quality_score
from core.ident import analyze

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

DEFAULT_CONFIG = str(resolve_config_path())


def build_client(cfg: AppConfig, config_path: str) -> GuangyaClient:
    def on_token_change(access: str, refresh: str) -> None:
        try:
            cfg.save_token(access, refresh, config_path)
        except Exception as exc:
            log.warning("写回令牌失败: %s", exc)

    return GuangyaClient(
        access_token=cfg.guangya.access_token,
        refresh_token=cfg.guangya.refresh_token,
        client_id=cfg.guangya.client_id,
        device_id=cfg.guangya.device_id,
        on_token_change=on_token_change,
    )


_OFFLINE_WAIT_TIMEOUT = 180   # 离线任务最多等 3 分钟（光鸭解析通常几十秒内完成）
_OFFLINE_POLL_INTERVAL = 15   # 轮询间隔秒数
_TASK_MONITOR_INTERVAL = 60   # 后台监控线程检查间隔


def _norm(s: str) -> str:
    """文件夹名归一化：去扩展名、去掉非中英文数字的字符、转小写。

    用于把「种子英文原名」和「云端实际文件夹名」拉到同一标准比对——
    两者常只差 .torrent / .mp4 后缀、或 WEB-DL 之类的额外尾巴。
    """
    s = (s or "").strip().lower()
    base = s.rsplit(".", 1)[0] if "." in s else s
    return re.sub(r"[^0-9a-z一-鿿]", "", base)


def _record_ledger(store: Store, text: str, category: str = "") -> None:
    """把「已成功落盘的内容」登记进内容账本（titles 表）。

    账本主键由标题的身份识别算出（同内容不同 hash/写法 → 同一 key），
    之后再来同片直接命中跳过。写失败只告警，不影响主流程。
    """
    try:
        if not text:
            return
        info = analyze(text)
        if not info.key:
            return
        store.add_title(TitleRecord(
            norm_key=info.key,
            norm_core=_norm(info.core),
            sig=info.sig,
            is_pack=info.is_pack,
            year=info.year,
            title=text[:200],
            folder=info.folder,
            category=category or "",
            quality=quality_score(text),
        ))
    except Exception as exc:  # noqa: BLE001 - 账本写入失败不能拖垮转存
        log.warning("写入内容账本失败: %s", exc)


def backfill_title_ledger(store: Store) -> int:
    """升级到 v1.3 后回填账本：把历史已成功（done/upgraded）的转存按新规则登记。

    否则老用户库里已转过的片，升级后遇到同片会被当新资源再转一份。
    """
    scanned = added = 0
    for status in ("done", "upgraded"):
        offset = 0
        while True:
            rows = store.history(limit=200, status=status, offset=offset)
            if not rows:
                break
            for rec in rows:
                if not rec.title:
                    continue
                before = store.title_count()
                _record_ledger(store, rec.title, rec.category)
                after = store.title_count()
                scanned += 1
                if after > before:
                    added += 1
            offset += len(rows)
            if len(rows) < 200:
                break
    if scanned:
        log.info("账本回填：扫描 %d 条历史转存，登记 %d 条内容（账本现有 %d 条）",
                 scanned, added, store.title_count())
    return added


def _rename_folder_to_cn(client: GuangyaClient, task_id: str, orig_name: str,
                         parent_id: str, cn_folder: str) -> bool:
    """把离线下载生成的【外层文件夹】重命名为中文名。返回 True 表示已处理。

    定位文件夹有两条路（关键是第 ② 条兜底）：
      ① 用离线任务返回的 fileId（并校验它确实是目标目录下的文件夹）
      ② 拿不到 fileId 时，按英文种子原名在目录下匹配文件夹

    之前只走 ①，一旦光鸭 list_task 不返回 fileId 就【静默跳过、一条日志都没有】，
    表现为「升级了却仍是英文名」。现在两条路都走，并且每步打日志便于排查。
    """
    try:
        entries = client.list_dir(parent_id)
    except GuangyaError as exc:
        log.warning("改名失败：无法列举目标目录 %s（保持英文原名 %s）: %s",
                    parent_id, orig_name, exc)
        return False
    folders = [e for e in entries if e.get("res_type") == 2]

    # 已经是中文名（创建时即生效）→ 无需再动
    if any(_norm(e.get("name")) == _norm(cn_folder) for e in folders):
        log.info("外层文件夹已是中文名（创建时即生效）: %s", cn_folder)
        return True

    fid = ""
    # ① 优先用离线任务返回的 fileId
    try:
        hit = next((t for t in client.list_tasks() if t.task_id == task_id and t.file_id), None)
    except GuangyaError:
        hit = None
    if hit:
        if any(e.get("file_id") == hit.file_id for e in folders):
            fid = hit.file_id
        else:
            log.info("改名诊断：离线任务 fileId=%s 不在目标目录内，改按英文原名匹配", hit.file_id)
    else:
        log.info("改名诊断：离线任务未返回 fileId，改按英文原名匹配")

    # ② 按英文原名在目标目录内匹配文件夹（兜底，不依赖 fileId）
    if not fid:
        want = _norm(orig_name)
        for e in folders:
            if _norm(e.get("name")) == want:
                fid = e.get("file_id")
                break
        if not fid:  # 子串兜底（云端名可能比种子名多 WEB-DL 之类尾巴）
            for e in folders:
                n = _norm(e.get("name"))
                if want and n and (want in n or n in want):
                    fid = e.get("file_id")
                    break
        log.info("改名诊断：目标目录内共 %d 个文件夹，英文原名 %r → 匹配结果 %s",
                 len(folders), orig_name, fid or "未匹配到")

    if not fid:
        log.warning("改名失败：未能定位外层文件夹（英文原名 %s），保持英文", orig_name)
        return False

    client.rename_file(fid, cn_folder)
    log.info("外层文件夹已重命名为中文: %s", cn_folder)
    return True


def submit_one(client: GuangyaClient, url: str, parent_id: str, max_retries: int,
               cn_title: str = "") -> tuple[bool, str, str, str, bool | None]:
    """提交单个链接到光鸭离线下载。

    返回 (ok, task_id, name, final_status_text, rename_ok)。
    rename_ok 为 None 表示未尝试改名（无中文标题），True 成功，False 失败。
    提交后会等待离线任务完成（最多 _OFFLINE_WAIT_TIMEOUT 秒），
    超时或失败时仍返回 ok=True（因为任务已创建，只是未完成）。
    """
    last_err = ""
    rename_ok: bool | None = None
    # 中文文件夹名（不带文件后缀）：创建时先尝试指定，完成后再校验 + rename 兜底
    cn_folder = build_cn_filename(cn_title) if cn_title else ""
    for attempt in range(1, max_retries + 1):
        try:
            task_id, name = client.create_offline_task(url, parent_id, cn_name=cn_folder)
            # 等待任务完成：解析资源通常很快，超过 3 分钟说明已经卡住
            log.info("提交离线任务 %s，等待完成（最多 %ds）...", task_id, _OFFLINE_WAIT_TIMEOUT)
            status_code, msg = client.wait_offline_task(
                task_id, timeout=_OFFLINE_WAIT_TIMEOUT, poll_interval=_OFFLINE_POLL_INTERVAL,
            )
            if status_code == GuangyaClient.STATUS_SUCCESS:
                log.info("任务 %s 完成: %s", task_id, msg)
                # 把离线下载生成的【外层文件夹】重命名为中文标题（不动里面的文件）
                if cn_folder:
                    try:
                        rename_ok = _rename_folder_to_cn(client, task_id, name, parent_id, cn_folder)
                    except GuangyaError as exc:
                        rename_ok = False
                        log.warning("中文文件夹重命名失败（保留原名 %s）: %s", name, exc)
                return True, task_id, name, "done", rename_ok
            if status_code in (GuangyaClient.STATUS_FAILED, GuangyaClient.STATUS_FAILED_ALT):
                log.warning("任务 %s 失败: %s", task_id, msg)
                return False, task_id, name, f"failed: {msg}", rename_ok
            # 超时或未结束：任务仍在进行中，视为提交成功
            log.info("任务 %s 仍在进行中: %s", task_id, msg)
            return True, task_id, name, "pending", rename_ok
        except GuangyaError as exc:
            last_err = str(exc)
            low = last_err.lower()
            if "次数" in last_err or "限额" in last_err or "quota" in low:
                return False, "", f"离线配额不足: {last_err}", "quota_exceeded", rename_ok
            if attempt < max_retries:
                time.sleep(min(30, attempt * 5))
    return False, "", last_err, "error", rename_ok


def make_handler(store: Store, client: GuangyaClient, flt: KeywordFilter,
                 notifier: Notifier, parent_id: str, max_retries: int,
                 classifier: Classifier | None = None,
                 resolver: CategoryResolver | None = None,
                 dedup: CloudDedup | None = None,
                 organize_enabled: bool = False):
    def pick_target(text: str) -> tuple[str, str]:
        """返回 (目标目录 fileId, 分类名)。未开启自动分类则用统一目录。"""
        if classifier is None or resolver is None:
            return parent_id, ""
        cr = classifier.classify(text)
        target, path = resolver.resolve(cr.category)
        log.info("分类: %s → %s（%s/%s，置信度 %.0f%%）",
                 text[:34], path or "根目录", cr.kind_name, cr.region_name,
                 cr.confidence * 100)
        return target, path or cr.category

    def handler(msg) -> None:
        for url in msg.links:
            h = link_key(url)
            ok, reason = flt.match(msg.text)
            if not ok:
                if not store.seen(h):
                    store.add(MagnetRecord(hash=h, channel=msg.channel, message_id=msg.message_id,
                                           title=msg.text[:120]))
                store.update(h, status="skipped", reason=reason)
                log.info("跳过（%s）: %s", reason, msg.text[:50])
                notifier.send(f"⏭️ 跳过/{reason}: {msg.text[:60]}")
                continue

            # 两级去重：本地记录 → 云端复查 → 中文规范准入
            if dedup is not None:
                d = dedup.decide(h, msg.text, store)
                if d.action == "reject":
                    # 落盘准入失败：做不到中文规范命名/整理归类 → 放弃这个链接
                    if not store.seen(h):
                        store.add(MagnetRecord(hash=h, channel=msg.channel,
                                               message_id=msg.message_id, title=msg.text[:120]))
                    store.update(h, status="skipped", reason=d.reason, category=d.category)
                    log.info("⛔ 放弃链接（%s）: %s", d.reason, msg.text[:50])
                    notifier.send(f"⛔ 已放弃（无法中文规范命名/归类）: {msg.text[:60]}")
                    continue
                if d.action == "skip_exists":
                    if not store.seen(h):
                        store.add(MagnetRecord(hash=h, channel=msg.channel,
                                               message_id=msg.message_id, title=msg.text[:120]))
                    store.update(h, status="skipped", reason=d.reason, category=d.category)
                    log.info("⏭️ 去重丢弃（%s）: %s", d.reason, msg.text[:50])
                    notifier.send(f"🔁 已存在，跳过: {msg.text[:60]}")
                    continue
                is_upgrade = False
                if d.action == "upgrade":
                    # 洗版：先删旧版本，再转存质量更优的新版本
                    if d.replace_file_id:
                        try:
                            client.delete_file(d.replace_parent_id, d.replace_file_id)
                            log.info("洗版：已删除旧版本 %s", d.replace_file_id)
                        except GuangyaError as exc:
                            log.warning("洗版删除旧版本失败（仍尝试转存新版本）: %s", exc)
                    is_upgrade = True
                cat = d.category
            else:
                is_upgrade = False
                cat = classifier.classify(msg.text).category if classifier else ""

            if not store.seen(h):
                store.add(MagnetRecord(hash=h, channel=msg.channel, message_id=msg.message_id,
                                       title=msg.text[:120]))
            target, category = pick_target(msg.text)
            ok2, task_id, name, final_status, rename_ok = submit_one(
                client, url, target, max_retries, cn_title=msg.text)
            if ok2:
                db_status = "done" if final_status == "done" else ("upgraded" if is_upgrade else "submitted")
                store.update(h, status=db_status,
                             task_id=task_id, category=category)
                # 真正落盘成功 → 记内容账本（后续同片不同磁力也能认出来）
                if final_status == "done":
                    _record_ledger(store, msg.text, category)
                parsed = parse_title(msg.text)
                where = f"→ {category}" if category else ""
                tag = "♻️ 洗版转存" if is_upgrade else "✅ 已转存"
                if final_status == "done":
                    tag += "（已完成）"
                # 改名状态追加到通知
                rename_hint = ""
                if rename_ok is True:
                    rename_hint = " 📁已改中文"
                elif rename_ok is False:
                    rename_hint = " ⚠️ 改名失败（保持英文）"
                log.info("%s %s: %s | 任务 %s [%s] rename=%s", tag, where, parsed.get("title") or msg.text[:40], task_id, final_status, rename_ok)
                notifier.send(f"{tag} {where}: {msg.text[:70]} (任务 {task_id}){rename_hint}")
            else:
                # 注意：此处原先引用了未定义的 msg_text，一旦提交失败就会抛
                # NameError 中断整轮处理。失败原因应取 submit_one 返回的 name
                # （失败时它是错误描述）与 final_status。
                reason = (name or final_status or "提交失败")[:200]
                store.update(h, status="failed", reason=reason)
                log.warning("❌ 提交失败: %s | %s", msg.text[:50], reason)
                notifier.send(f"❌ 失败: {msg.text[:60]} | {reason[:80]}")
    return handler


def start_task_monitor(store: Store, client: GuangyaClient, notifier: Notifier) -> threading.Thread:
    """后台线程：定期轮询所有 submitted 任务的状态，更新数据库记录。

    解决「提交后任务实际失败但状态仍为 submitted」的问题——
    主流程 submit_one 会等待（最多 180s），但超时的任务仍留在 submitted 状态，
    此监控线程会持续检查直到它们进入 done/failed。
    """
    def _loop() -> None:
        while True:
            try:
                # 只查 pending/running 的任务（不需要反复查已完成的）
                pending_tasks = client.list_tasks(statuses=[0, 1, 4])
                task_map = {t.task_id: t for t in pending_tasks}
                if not task_map:
                    time.sleep(_TASK_MONITOR_INTERVAL)
                    continue
                # 找出数据库中 submitted 且当前仍在列表中的任务
                rows = store.history(limit=200)
                updated = 0
                for rec in rows:
                    if rec.status not in ("submitted", "upgraded"):
                        continue
                    tid = (rec.task_id or "").strip()
                    if tid not in task_map:
                        # 任务已从光鸭侧清除（可能是用户手动删除），标记 failed
                        store.update(tid, status="failed", reason="任务被清除（可能手动删除）")
                        updated += 1
                        continue
                    t = task_map[tid]
                    if t.status == GuangyaClient.STATUS_SUCCESS:
                        store.update(tid, status="done")
                        # 提交时超时、实际在后台才完成的任务，落盘成功也要补记账本
                        _record_ledger(store, rec.title or "", rec.category or "")
                        updated += 1
                        log.info("任务 %s 已完成", tid)
                    elif t.status in (GuangyaClient.STATUS_FAILED, GuangyaClient.STATUS_FAILED_ALT):
                        store.update(tid, status="failed", reason=t.message or "离线下载失败")
                        updated += 1
                        log.warning("任务 %s 失败: %s", tid, t.message)
                    elif t.status == GuangyaClient.STATUS_RUNNING:
                        # 仍在下载，不更新
                        pass
                if updated:
                    log.info("任务状态监控更新 %d 条记录", updated)
            except Exception as exc:
                log.warning("任务监控循环异常: %s", exc)
            time.sleep(_TASK_MONITOR_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="task_monitor")
    t.start()
    log.info("任务状态监控已启动（间隔 %ds）", _TASK_MONITOR_INTERVAL)
    return t


def start_discovery(cfg: AppConfig, config_path: str, scraper: WebScraper | None = None) -> ChannelDiscovery | None:
    """启动后台频道自动发现线程；未启用则返回 None。

    on_new 回调：把新频道写进配置文件，并（若使用网页模式）实时同步给抓取器。
    """
    d = cfg.discovery
    if not d.enabled or (not d.seed_urls and not d.seed_file):
        return None

    disc = ChannelDiscovery(
        seed_urls=d.seed_urls, seed_file=d.seed_file, interval_hours=d.interval_hours,
        proxy=cfg.source.proxy, verify_threshold=d.verify_threshold,
    )
    disc.load_known(cfg.source.channels)

    def on_new(new: set[str]) -> None:
        try:
            cfg.add_channels(sorted(new), config_path)
            if scraper is not None:
                scraper.channels = cfg.source.channels  # 让网页抓取实时生效
        except Exception as exc:
            log.warning("追加频道失败: %s", exc)

    threading.Thread(target=disc.run, args=(on_new,), daemon=True, name="discovery").start()
    return disc


_STATUS_ICON = {
    "done": "✅", "submitted": "⏳", "upgraded": "♻️",
    "failed": "❌", "skipped": "⏭️", "pending": "⏳",
}


def _fmt_size(n: int) -> str:
    """字节转人类可读大小。"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return "-"


def start_bot(cfg: AppConfig, config_path: str, store: Store, client: GuangyaClient,
              handler, scraper: WebScraper | None, notifier: Notifier) -> TgBot | None:
    """启动 TG 机器人（可选）：命令交互 + 转存结果推送。

    未启用或没填 token 时返回 None，不影响原有流程。
    所有动作都复用主流程的 handler，保证机器人提交的链接和频道抓到的
    走完全相同的过滤 / 去重 / 分类 / 洗版逻辑。
    """
    b = cfg.bot
    if not b.enabled or not b.token:
        return None

    def _run_handler(msg: BotMessage) -> None:
        """在独立线程里跑提交——离线任务可能要等几分钟，不能卡住机器人。"""
        try:
            handler(msg)
        except Exception as exc:  # noqa: BLE001 - 机器人侧的异常不能拖垮主流程
            log.warning("机器人提交的链接处理失败: %s", exc)
            notifier.send(f"❌ 机器人提交处理失败: {exc}")

    def _submit(text: str, chat_id: int) -> str:
        links = extract_links(text or "")
        if not links:
            return ("没识别到可下载的链接。\n"
                    "支持：磁力 magnet: / 迅雷 thunder: / 电驴 ed2k: / http 直链。")
        msg = BotMessage(links=links, text=(text or "")[:200],
                         channel="tgbot", message_id=str(chat_id))
        threading.Thread(target=_run_handler, args=(msg,),
                         daemon=True, name="bot-submit").start()
        return f"📥 收到 {len(links)} 个链接，已开始提交（结果稍后推送）。"

    def _status() -> str:
        try:
            tasks = client.list_tasks()
        except GuangyaError as exc:
            return f"查询失败：{exc}"
        if not tasks:
            return "当前没有离线任务。"
        running = [t for t in tasks if not t.finished]
        ok = [t for t in tasks if t.status == GuangyaClient.STATUS_SUCCESS]
        bad = [t for t in tasks if t.finished and not t.ok]
        lines = [f"📊 进行中 {len(running)} ｜ 完成 {len(ok)} ｜ 失败 {len(bad)}"]
        for t in running[:8]:
            name = (t.name or "未命名")[:28]
            lines.append(f"⏳ {name} — {t.progress}% · {_fmt_size(t.size)} "
                         f"· {STATUS_TEXT.get(t.status, '')}")
        for t in bad[:4]:
            lines.append(f"❌ {(t.name or '未命名')[:28]} — {(t.message or '失败')[:30]}")
        if len(running) > 8:
            lines.append(f"（进行中还有 {len(running) - 8} 条未列）")
        return "\n".join(lines)

    def _stats() -> str:
        st = store.stats()
        total = sum(st.values())
        if not total:
            return "还没有任何记录。"
        order = ["done", "upgraded", "submitted", "skipped", "failed", "pending"]
        parts = [f"{_STATUS_ICON.get(k, '•')}{k} {st[k]}"
                 for k in order if st.get(k)]
        other = [f"•{k} {v}" for k, v in st.items() if k not in order]
        return "📈 *转存统计*\n\n共 %d 条\n%s" % (total, "\n".join(parts + other))

    def _pause(want_pause: bool) -> str:
        if scraper is None:
            return "当前是 userbot 模式，不支持暂停/恢复。"
        if want_pause:
            scraper.pause_event.set()
            return "⏸ 已暂停频道轮询（机器人里提交的链接照常处理）。"
        scraper.pause_event.clear()
        return "▶️ 已恢复频道轮询。"

    def _channels() -> list[str]:
        return list(cfg.source.channels)

    def _sync_scraper() -> None:
        """配置改动后同步给正在运行的抓取器，否则要重启才生效。"""
        if scraper is not None:
            scraper.channels = [WebScraper._normalize(c) for c in cfg.source.channels]

    def _add(name: str) -> str:
        n = cfg.add_channels([name], config_path)
        if n == 0:
            return f"频道 `{name}` 已在列表里。"
        _sync_scraper()
        return f"➕ 已添加 `{name}`（共 {len(cfg.source.channels)} 个频道）"

    def _del(name: str) -> str:
        before = len(cfg.source.channels)
        cfg.source.channels = [c for c in cfg.source.channels
                               if str(c).strip().lower() != name.lower()]
        if len(cfg.source.channels) == before:
            return f"频道 `{name}` 不在列表里。"
        try:
            cfg.save(config_path)
        except Exception as exc:  # noqa: BLE001
            return f"写回配置失败：{exc}"
        _sync_scraper()
        return f"➖ 已删除 `{name}`（剩 {len(cfg.source.channels)} 个频道）"

    def _find(kw: str) -> str:
        try:
            rows = store.history(limit=500)
        except Exception as exc:  # noqa: BLE001
            return f"查询失败：{exc}"
        low = kw.lower()
        hits = [r for r in rows if low in (r.title or "").lower()]
        if not hits:
            return f"没找到包含「{kw}」的记录。"
        lines = [f"🔍 「{kw}」命中 {len(hits)} 条（显示前 12）"]
        for r in hits[:12]:
            icon = _STATUS_ICON.get(r.status, "•")
            cat = f" → {r.category}" if r.category else ""
            lines.append(f"{icon} {(r.title or '未命名')[:38]}{cat}")
        return "\n".join(lines)

    def _search(kw: str, limit: int = 8):
        """全网磁力搜索（海盗湾 apibay API）。返回 (payload, 错误摘要)。"""
        from core import magnet_search
        if not cfg.bot.search_enabled:
            return [], "全网磁力搜索已在配置里关闭（bot.search_enabled=false）。"
        try:
            hits, errors = magnet_search.search_all(
                kw, engines=cfg.bot.search_engines or ["apibay"], limit=max(1, int(limit)),
                proxy=cfg.bot.proxy or cfg.source.proxy)
        except Exception as exc:  # noqa: BLE001
            log.warning("全网磁力搜索异常: %s", exc)
            return [], f"搜索出错：{exc}"
        return magnet_search.to_payload(hits), "；".join(errors)

    bot = TgBot(
        token=b.token,
        admin_ids=b.admin_ids,
        proxy=b.proxy or cfg.source.proxy,
        allow_anyone=b.allow_anyone,
        on_submit=_submit,
        on_status=_status,
        on_stats=_stats,
        on_pause=_pause,
        on_channels=_channels,
        on_add_channel=_add,
        on_del_channel=_del,
        on_find=_find,
        on_search=_search,
    )
    # 转存结果推送给管理员（与控制台通知并行，互不影响）
    if b.notify:
        notifier.on_message(bot.notify)
    bot.start_thread()
    log.info("TG 机器人已启用（管理员 %d 位，通知=%s）",
             len(b.admin_ids), "开" if b.notify else "关")
    return bot


def run_web(cfg: AppConfig, scraper: WebScraper, handler, on_prune=None) -> None:
    if cfg.scan_history:
        log.info("扫描历史消息（%d 页）...", cfg.history_pages)
        for ch in cfg.source.channels:
            try:
                for m in scraper.iter_history(ch, cfg.history_pages):
                    if m.links:
                        handler(m)
            except Exception as exc:
                log.warning("历史扫描 %s 出错: %s", ch, exc)
    log.info("开始轮询频道（间隔 %ds）...", cfg.source.poll_interval)
    scraper.poll_forever(handler, max_consecutive_failures=3, on_prune=on_prune)


def run_userbot(cfg: AppConfig, handler) -> None:
    if not cfg.telegram.api_id or not cfg.telegram.api_hash:
        raise SystemExit("userbot 模式需要在 config.yaml 配置 telegram.api_id / api_hash")
    src = UserbotSource(
        cfg.telegram.api_id, cfg.telegram.api_hash,
        cfg.telegram.session, cfg.source.channels,
        proxy=cfg.source.proxy,
    )
    src.on_message(handler)
    src.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="TG 频道资源自动转存到光鸭云盘")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    args = ap.parse_args()

    cfg = AppConfig.load(args.config)
    # 所有持久化文件收敛到数据目录（/data 或本地 data/），避免重建容器丢数据
    data_dir = get_data_dir()
    cfg.storage_db = resolve_rel(data_dir, cfg.storage_db)
    cfg.telegram.session = resolve_rel(data_dir, cfg.telegram.session)
    store = Store(cfg.storage_db)
    # 升级回填：老用户历史成功转存按新规则登记进账本，防止升级后被重复转存
    try:
        backfill_title_ledger(store)
    except Exception as exc:  # noqa: BLE001 - 回填失败不阻塞启动
        log.warning("账本回填失败（可稍后手动触发）: %s", exc)
    client = build_client(cfg, args.config)

    if not client.token:
        log.warning("未检测到光鸭令牌，启动扫码登录...")
        try:
            client.login_interactive()
        except Exception as exc:
            raise SystemExit(f"扫码登录失败: {exc}")

    flt = KeywordFilter(cfg.filter.include_keywords, cfg.filter.exclude_keywords, cfg.filter.min_resolution)
    notifier = Notifier(console=cfg.notify_console)
    parent_id = cfg.output.parent_id or cfg.output.save_path

    # 自动分类：按「内容形态 + 地区」自动建子目录再转存进去
    classifier = None
    resolver = None
    if cfg.organize.enabled:
        classifier = Classifier(
            mapping=cfg.organize.mapping or None,
            structure=cfg.organize.structure,
            unknown_dir=cfg.organize.unknown_dir,
        )
        resolver = CategoryResolver(client, root_id=parent_id,
                                    create_missing=cfg.organize.create_missing)
        log.info("自动分类已开启（结构=%s，目录不存在时自动创建=%s）",
                 cfg.organize.structure, cfg.organize.create_missing)

    # 两级去重：本地记录 + 云端复查
    dedup = CloudDedup(
        client, resolver or CategoryResolver(client, root_id=parent_id, create_missing=False),
        classifier or Classifier(),
        cloud_check_new=cfg.dedup.cloud_check_new,
        cache_ttl=cfg.dedup.cache_ttl,
        organize_enabled=cfg.organize.enabled,
        upgrade=cfg.dedup.upgrade,
        require_cn=cfg.dedup.require_cn,
    )
    log.info("转存去重已开启（云端复查=%s，结构=%s）",
             "开" if cfg.dedup.cloud_check_new else "关（仅本地 hash 去重）",
             cfg.organize.structure if cfg.organize.enabled else "单目录")

    handler = make_handler(store, client, flt, notifier, parent_id, cfg.max_retries,
                           classifier=classifier, resolver=resolver, dedup=dedup,
                           organize_enabled=cfg.organize.enabled)

    # 来源对象（网页模式下，其频道列表会随自动发现实时更新）
    source_obj = None
    if cfg.source.type != "userbot":
        source_obj = WebScraper(cfg.source.channels, interval=cfg.source.poll_interval, proxy=cfg.source.proxy)

    # 后台自动发现频道（web / userbot 模式通用，只往配置里加）
    disc = start_discovery(cfg, args.config, scraper=source_obj)

    # 后台任务状态监控（持续更新 submitted → done/failed）
    task_monitor = start_task_monitor(store, client, notifier)

    # TG 机器人（可选）：命令交互 + 结果推送。未启用时返回 None，不影响原流程。
    start_bot(cfg, args.config, store, client, handler, source_obj, notifier)

    log.info("配置加载完成 | 频道 %d 个 | 来源=%s | 自动发现=%s | 自动分类=%s",
             len(cfg.source.channels), cfg.source.type, "开" if disc else "关",
             "开" if resolver else "关")
    # 零产出频道自动剔除：把被判定为"纯噪音"的频道从配置里移除（写回配置文件）
    def _on_prune(ch: str) -> None:
        try:
            cfg.source.channels = [
                c for c in cfg.source.channels
                if str(c).strip().lower() != ch.lower()
            ]
            cfg.save(args.config)
            log.info("自动剔除零产出频道: %s（已写回 %s）", ch, args.config)
        except Exception as e:
            log.warning("剔除频道写回配置失败 %s: %s", ch, e)

    try:
        if cfg.source.type == "userbot":
            run_userbot(cfg, handler)
        else:
            run_web(cfg, source_obj, handler, on_prune=_on_prune)
    except KeyboardInterrupt:
        log.info("收到中断信号")
    finally:
        if disc:
            disc.stop()
        store.close()


if __name__ == "__main__":
    main()
