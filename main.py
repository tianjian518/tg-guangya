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
import threading
import time

from adapters.web_scraper import WebScraper, link_key, extract_links
from adapters.userbot import UserbotSource
from core.guangya import GuangyaClient, GuangyaError
from core.store import Store, MagnetRecord
from core.matcher import KeywordFilter, parse_title
from core.notifier import Notifier
from core.config import AppConfig
from core.discovery import ChannelDiscovery
from core.data_dir import resolve_config_path, get_data_dir, resolve_rel
from core.classifier import Classifier
from core.organizer import CategoryResolver
from core.dedup import CloudDedup

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


def submit_one(client: GuangyaClient, url: str, parent_id: str, max_retries: int) -> tuple[bool, str, str]:
    """提交单个链接到光鸭离线下载。返回 (ok, task_id, message)。"""
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            task_id, name = client.create_offline_task(url, parent_id)
            return True, task_id, name
        except GuangyaError as exc:
            last_err = str(exc)
            low = last_err.lower()
            if "次数" in last_err or "限额" in last_err or "quota" in low:
                return False, "", f"离线配额不足: {last_err}"
            if attempt < max_retries:
                time.sleep(min(30, attempt * 5))
    return False, "", last_err


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

            # 两级去重：本地记录 → 云端复查
            if dedup is not None:
                d = dedup.decide(h, msg.text, store)
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
            ok2, task_id, msg_text = submit_one(client, url, target, max_retries)
            if ok2:
                store.update(h, status="upgraded" if is_upgrade else "submitted",
                             task_id=task_id, category=category)
                parsed = parse_title(msg.text)
                where = f"→ {category}" if category else ""
                tag = "♻️ 洗版转存" if is_upgrade else "✅ 已转存"
                log.info("%s %s: %s | 任务 %s", tag, where, parsed.get("title") or msg.text[:40], task_id)
                notifier.send(f"{tag} {where}: {msg.text[:70]} (任务 {task_id})")
            else:
                store.update(h, status="failed", reason=msg_text)
                log.warning("❌ 提交失败: %s | %s", msg.text[:50], msg_text)
                notifier.send(f"❌ 失败: {msg.text[:60]} | {msg_text[:80]}")
    return handler


def start_discovery(cfg: AppConfig, config_path: str, scraper: WebScraper | None = None) -> ChannelDiscovery | None:
    """启动后台频道自动发现线程；未启用则返回 None。

    on_new 回调：把新频道写进配置文件，并（若使用网页模式）实时同步给抓取器。
    """
    d = cfg.discovery
    if not d.enabled or (not d.seed_urls and not d.seed_file):
        return None

    disc = ChannelDiscovery(
        seed_urls=d.seed_urls, seed_file=d.seed_file, interval_hours=d.interval_hours
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


def run_web(cfg: AppConfig, scraper: WebScraper, handler) -> None:
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
    scraper.poll_forever(handler)


def run_userbot(cfg: AppConfig, handler) -> None:
    if not cfg.telegram.api_id or not cfg.telegram.api_hash:
        raise SystemExit("userbot 模式需要在 config.yaml 配置 telegram.api_id / api_hash")
    src = UserbotSource(
        cfg.telegram.api_id, cfg.telegram.api_hash,
        cfg.telegram.session, cfg.source.channels,
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
        source_obj = WebScraper(cfg.source.channels, interval=cfg.source.poll_interval)

    # 后台自动发现频道（web / userbot 模式通用，只往配置里加）
    disc = start_discovery(cfg, args.config, scraper=source_obj)

    log.info("配置加载完成 | 频道 %d 个 | 来源=%s | 自动发现=%s | 自动分类=%s",
             len(cfg.source.channels), cfg.source.type, "开" if disc else "关",
             "开" if resolver else "关")
    try:
        if cfg.source.type == "userbot":
            run_userbot(cfg, handler)
        else:
            run_web(cfg, source_obj, handler)
    except KeyboardInterrupt:
        log.info("收到中断信号")
    finally:
        if disc:
            disc.stop()
        store.close()


if __name__ == "__main__":
    main()
