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
from core.guangya import (
    GuangyaClient,
    GuangyaError,
    GuangyaBizError,
    STATUS_TEXT,
    parse_share_url,
)
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
from core.ident import analyze, show_folder

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
    ⚠️ 只可用于「同一资源的两个写法」对位比对（orig_name ↔ 产物名），
    不可用于「目标中文名 vs 目录内任意条目」——rsplit 会把「夏季.2026」
    和「夏季.S01E05」都退化成「夏季」，造成跨条目误判（历史 bug）。
    """
    s = (s or "").strip().lower()
    base = s.rsplit(".", 1)[0] if "." in s else s
    return re.sub(r"[^0-9a-z一-鿿]", "", base)


# 视频/种子扩展名：只用于「条目名去扩展名」，不碰名字中段的点（如 夏季.2026）
_VIDEO_EXT_RE = re.compile(
    r"\.(mkv|mp4|avi|ts|rmvb|rm|iso|mov|wmv|flv|m2ts|torrent)$", re.I)


def _norm_full(s: str) -> str:
    """全量归一化：lower + 只留字母数字中文，【不砍】名字尾部的点段。

    「夏季.2026」→ 夏季2026、「夏季.S01E05」→ 夏季s01e05：两者必须可区分。
    凡是拿「目标中文名」去比对目录内任意条目，都必须用这一套（_entry_key）。
    """
    return re.sub(r"[^0-9a-z一-鿿]", "", (s or "").strip().lower())


def _entry_key(name: str) -> str:
    """云端条目的比对键：先去视频扩展名，再全量归一化。

    文件夹名原样（无扩展名可去）；单集文件去掉 .mkv 等尾巴后与 cn_folder 同规。
    """
    return _norm_full(_VIDEO_EXT_RE.sub("", name or ""))


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


def _ensure_subdir(client: GuangyaClient, parent_id: str, name: str) -> str:
    """在 parent_id 下找名为 name 的子目录，没有则创建。返回其 fileId。"""
    try:
        for e in client.list_dir(parent_id):
            if e.get("res_type") == 2 and (e.get("name") or "").strip() == name:
                return e.get("file_id") or ""
    except GuangyaError:
        pass
    return client.create_folder(parent_id, name)


def _rename_artifact_to_cn(client: GuangyaClient, task_id: str, orig_name: str,
                           parent_id: str, cn_folder: str, show_dir: str = "") -> bool:
    """把离线下载产物（外层文件夹或单文件）重命名为中文名。返回 True 表示已处理。

    产物有两种形态（均来自真实网盘观察，2026-09）：
      - BT 磁力 → 文件夹（res_type=2），名字为种子名，无扩展名
      - HTTP 直链 / 单文件种子 → 单个文件（res_type=1），名字带 .mkv 等扩展名
    对文件改名时保留原扩展名（如 测试电影.2024.mkv），否则光鸭可能不识别媒体类型。

    剧集收纳（show_dir 非空时）：一部电视剧一个文件夹。单集文件改名后
    move 进 分类目录/show_dir/（如 国产剧/夏季.2026/夏季.S01E04.mkv），
    避免不同剧集的单集在分类目录里平铺混在一起。show_dir 不存在会自动创建。
    BT 整包本来就是文件夹，改完名即自成一部剧的文件夹，无需 move。

    定位产物有两条路（关键是第 ② 条兜底）：
      ① 用离线任务返回的 fileId（并校验它确实是目标目录下的产物）
      ② 拿不到 fileId 时，按英文种子原名在目录下匹配
        （_norm 会抹平扩展名/WEB-DL 尾巴差异，文件与文件夹统一比对）

    之前只走 ①，一旦光鸭 list_task 不返回 fileId 就【静默跳过、一条日志都没有】；
    之前也只匹配文件夹，单文件产物【永远改不了名】。两条路 + 两种形态都覆盖。
    """
    try:
        entries = client.list_dir(parent_id)
    except GuangyaError as exc:
        log.warning("改名失败：无法列举目标目录 %s（保持英文原名 %s）: %s",
                    parent_id, orig_name, exc)
        return False
    artifacts = [e for e in entries if e.get("res_type") in (1, 2)]

    # 已经是中文名 → 无需再动。必须用「不砍尾巴」的 _entry_key 比对：
    # 用 _norm 会把「夏季.2026」（目录里已有的剧名文件夹）和「夏季.S01E05」
    # （本集目标名）都退化成「夏季」→ 误判为本集已处理 → 改名+收纳全被跳过，
    # 第二集永远进不了剧名文件夹（场景7 bug）。
    if any(_entry_key(e.get("name")) == _norm_full(cn_folder) for e in artifacts):
        log.info("产物已是中文名（创建时即生效）: %s", cn_folder)
        return True

    fid = ""
    ext = ""
    # ① 优先用离线任务返回的 fileId
    try:
        hit = next((t for t in client.list_tasks() if t.task_id == task_id and t.file_id), None)
    except GuangyaError:
        hit = None
    if hit:
        m = next((e for e in artifacts if e.get("file_id") == hit.file_id), None)
        if m:
            fid = hit.file_id
            name_e = m.get("name") or ""
            if m.get("res_type") == 1 and "." in name_e:  # 单文件 → 记住原扩展名
                ext = name_e.rsplit(".", 1)[1]
        else:
            log.info("改名诊断：离线任务 fileId=%s 不在目标目录内，改按英文原名匹配", hit.file_id)
    else:
        log.info("改名诊断：离线任务未返回 fileId，改按英文原名匹配")

    # ② 按英文原名在目标目录内匹配产物（兜底，不依赖 fileId）
    if not fid:
        want = _norm(orig_name)
        for e in artifacts:
            if _norm(e.get("name")) == want:
                fid = e.get("file_id")
                name_e = e.get("name") or ""
                if e.get("res_type") == 1 and "." in name_e:
                    ext = name_e.rsplit(".", 1)[1]
                break
        if not fid:  # 子串兜底（云端名可能比种子名多 WEB-DL 之类尾巴）
            for e in artifacts:
                n = _norm(e.get("name"))
                if want and n and (want in n or n in want):
                    fid = e.get("file_id")
                    name_e = e.get("name") or ""
                    if e.get("res_type") == 1 and "." in name_e:
                        ext = name_e.rsplit(".", 1)[1]
                    break
        log.info("改名诊断：目标目录内共 %d 个产物，英文原名 %r → 匹配结果 %s",
                 len(artifacts), orig_name, fid or "未匹配到")

    if not fid:
        log.warning("改名失败：未能定位产物（英文原名 %s），保持英文", orig_name)
        return False

    target_name = f"{cn_folder}.{ext}" if ext else cn_folder
    client.rename_file(fid, target_name)
    # 校验：重新列举目录，确认中文名已真正生效（防止接口静默忽略）
    try:
        entries = client.list_dir(parent_id)
    except GuangyaError:
        entries = []
    if not any(_entry_key(e.get("name")) == _entry_key(target_name)
               and e.get("file_id") == fid for e in entries if e.get("res_type") in (1, 2)):
        log.warning("改名后校验失败：目录中未找到 %s，保持英文原名", target_name)
        return False

    # 剧集收纳：单集文件 → move 进剧名文件夹（一部电视剧一个文件夹）
    if show_dir and ext:  # ext 非空 ⇔ 产物是单文件（文件夹产物 ext=""）
        try:
            show_id = _ensure_subdir(client, parent_id, show_dir)
            client.move_file(fid, show_id)
            inner = [e.get("name") for e in client.list_dir(show_id)]
            if target_name in inner:
                log.info("单集已收进剧名文件夹: %s/%s", show_dir, target_name)
                return True
            log.warning("移入剧名文件夹后校验失败（%s 未出现在 %s）", target_name, show_dir)
            return False
        except GuangyaError as exc:
            log.warning("剧集收纳失败（文件留在分类目录）: %s", exc)
            return True  # 改名已生效，仅收纳失败，不算整体失败
    log.info("产物已重命名为中文: %s", target_name)
    return True


def submit_one(client: GuangyaClient, url: str, parent_id: str, max_retries: int,
               cn_title: str = "") -> tuple[bool, str, str, str, bool | None, str]:
    """提交单个链接到光鸭离线下载。

    返回 (ok, task_id, name, final_status_text, rename_ok, cn_folder)。
    rename_ok 为 None 表示未尝试改名（无中文标题），True 成功，False 失败。
    提交后会等待离线任务完成（最多 _OFFLINE_WAIT_TIMEOUT 秒），
    超时或失败时仍返回 ok=True（因为任务已创建，只是未完成）。
    """
    last_err = ""
    rename_ok: bool | None = None
    # 中文文件夹名（不带文件后缀）：创建时先尝试指定，完成后再校验 + rename 兜底
    cn_folder = build_cn_filename(cn_title) if cn_title else ""
    # 剧集的剧名文件夹（剧名.年份，无集数）：单集文件完成后要收进这个文件夹
    show_dir = ""
    if cn_title:
        try:
            cand = show_folder(cn_title)
            info = analyze(cn_title)
            if info.sig and cand and cand != cn_folder:
                show_dir = cand
        except Exception:  # noqa: BLE001 - 剧名文件夹算不出不影响主流程
            show_dir = ""
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
                # 把离线下载生成的【外层产物】重命名为中文标题（剧集单集再收进剧名文件夹）
                if cn_folder:
                    try:
                        rename_ok = _rename_artifact_to_cn(client, task_id, name, parent_id,
                                                           cn_folder, show_dir=show_dir)
                    except GuangyaError as exc:
                        rename_ok = False
                        log.warning("中文文件夹重命名失败（保留原名 %s）: %s", name, exc)
                return True, task_id, name, "done", rename_ok, cn_folder
            if status_code in (GuangyaClient.STATUS_FAILED, GuangyaClient.STATUS_FAILED_ALT):
                log.warning("任务 %s 失败: %s", task_id, msg)
                return False, task_id, name, f"failed: {msg}", rename_ok, cn_folder
            # 超时或未结束：任务仍在进行中，视为提交成功
            log.info("任务 %s 仍在进行中: %s", task_id, msg)
            return True, task_id, name, "pending", rename_ok, cn_folder
        except GuangyaError as exc:
            last_err = str(exc)
            low = last_err.lower()
            if "次数" in last_err or "限额" in last_err or "quota" in low:
                return False, "", f"离线配额不足: {last_err}", "quota_exceeded", rename_ok, cn_folder
            if attempt < max_retries:
                time.sleep(min(30, attempt * 5))
    return False, "", last_err, "error", rename_ok, cn_folder


def _extract_share_title(text: str, url: str) -> str:
    """从频道消息里抠分享标题。

    真实频道消息形态（2026-09 光鸭资源频道实测）：
      「交锋--首更至3集-4稍后--无任何广-4K\n🅶https://…」   ← 标题在第一行，链接前是 emoji
      「原盘影视：李小龙电影 复制链接到「光鸭APP」内观看和转存。\n链接：https://…」
      「囧徒之预演告别 更10集 4KMAX画质 3G/集中文字幕 无广告 纯净\n光鸭云盘https://…」
      「「李熊猫」，链接：https://…」
    策略：取链接前文本的**第一个非空行**（频道消息标题几乎总在第一行），
    再剥栏目前缀（原盘影视：）与引导尾巴（复制链接到…/链接：），书名号装饰一并剥。
    第一行剥空则回退「最后一段」逻辑，再兜底原文（ident 的噪声剥离兜底）。
    """
    if not text or not url:
        return text or ""
    m = re.search(r"https?://\S*guangyapan\.com/(?:share|s)/", text, re.I)
    if not m:
        return text
    head = text[:m.start()].strip()
    if not head:
        return text

    def _clean(line: str) -> str:
        line = re.sub(r"(链接|地址|直链)\s*[:：]\s*$", "", line).strip()
        # 栏目前缀：「原盘影视：李小龙电影」→ 李小龙电影（≤8 字冒号头视为栏目）
        line = re.sub(r"^[「『【]?[^「『【】』」：:]{1,8}[：:]\s*", "", line).strip()
        # 引导尾巴：复制链接到「光鸭APP」内观看和转存。/ 打开链接 / 紧贴链接的「光鸭云盘」
        line = re.sub(r"(复制链接|打开链接|链接|光鸭云盘).*$", "", line).strip()
        # 首尾书名号/方括号装饰（保留圆括号——「遮天 (2023)」的括号是年份内容）
        line = re.sub(r"^[「『【【]+|[」』】】]+$", "", line).strip()
        # 剥「链接：」尾后残留的悬挂标点/书名号（仙逆，→ 仙逆；李熊猫」→ 李熊猫）
        # ] 放字符集首位防止提前闭合
        line = re.sub(r"[]」』】，,。．、；;：:!！?？…\s]+$", "", line).strip()
        return line

    # 装饰行判定：剥掉书名号/方括号包裹段与全部符号后几乎不剩正文
    # （真实频道：「♥♥♥♥♥【国漫】【持续更新，敬请收藏】♥♥♥♥♥」）
    # 注意必须分两步：[\W_] 会吞掉【破坏括号段匹配（♥♥♥♥♥【 被一次吃掉），
    # 先剥完整括号段、再剥残余符号。
    def _decorative(line: str) -> bool:
        bare = re.sub(r"【[^】]*】|「[^」]*」|『[^』]*』|\[[^\]]*\]", "", line)
        bare = re.sub(r"[\W_]+", "", bare)
        return len(bare) < 2

    # ① 第一个非装饰的非空行（频道标题几乎总在第一行，装饰行跳过）
    for line in head.split("\n"):
        if _decorative(line.strip()):
            continue
        cand = _clean(line.strip())
        if cand and re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cand):
            return cand
    # ② 回退：按标点切分取最后有效段（装饰词都在前面，标题紧贴链接）
    parts = [p.strip() for p in re.split(r"[，,。；;\n\r\t]+", head) if p.strip()]
    for cand in reversed(parts):
        cand = _clean(cand)
        if cand and re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cand):
            return cand
    return text


def _rename_verified(client: GuangyaClient, file_id: str, target_name: str,
                     parent_id: str) -> bool:
    """改名 + 复核重试。

    restore 刚完成时服务端副本元数据可能未稳定，rename 会静默丢失
    （HTTP 成功但名字没变，实测发生在四骑士 18GB 文件夹上）。
    改名后回头 list_dir 核对，未生效就重试，最多 3 轮。
    """
    for attempt in range(3):
        try:
            client.rename_file(file_id, target_name)
        except GuangyaError as exc:
            log.warning("分享产物改名请求失败（第 %d 次）: %s", attempt + 1, exc)
            time.sleep(2)
            continue
        # 立即复核：大多数情况 rename 同步生效，命中即零等待
        try:
            cur = next((x.get("name") for x in client.list_dir(parent_id)
                        if x.get("file_id") == file_id), None)
        except GuangyaError:
            cur = None
        if cur == target_name:
            return True
        log.info("分享产物改名未生效（现名 %r），等待后重试第 %d 次", cur, attempt + 1)
        time.sleep(2)  # 竞态：副本元数据未稳定，等一等再试
    return False


def _organize_share_entries(client: GuangyaClient, parent_id: str, before_ids: set[str],
                            new_count: int, cn_folder: str, show_dir: str = "") -> bool:
    """分享转存后的中文命名收纳（分门别类落盘的"命名"半边）。

    转存是服务端新建副本：分享内条目的 fileId 不会出现在自己网盘里，
    所以用「转存前后 list_dir(parent_id) 的差集」定位新条目，再：
      - 1 个新条目：与离线链路同款口径——单文件保留扩展名改成 cn_folder；
        剧集（show_dir 非空且是单文件）改名后收进剧名文件夹；文件夹产物直接改名。
      - 多个新条目：解析得出剧名文件夹时整批收进去；否则建 cn_folder
        文件夹归拢（分享是多文件合集，平铺会污染分类目录）。
    返回 True 表示至少完成一次有效改名/收纳。
    """
    try:
        after = client.list_dir(parent_id)
    except GuangyaError as exc:
        log.warning("分享收纳失败：无法列举目标目录: %s", exc)
        return False
    new_entries = [e for e in after
                   if e.get("res_type") in (1, 2) and e.get("file_id") not in before_ids]
    if not new_entries:
        # 差集为空可能是目录本来就空/列表分页差异，退而用数量兜底提示
        log.warning("分享收纳：未在目标目录发现新条目（分享根应有 %d 条）", new_count)
        return False
    if not cn_folder:
        log.info("分享收纳：标题解析不出规范中文名，保持分享原名（%d 个条目）", len(new_entries))
        return False

    try:
        if len(new_entries) == 1:
            e = new_entries[0]
            name_e = e.get("name") or ""
            ext = ""
            if e.get("res_type") == 1 and "." in name_e:
                ext = name_e.rsplit(".", 1)[1]
            target_name = f"{cn_folder}.{ext}" if ext else cn_folder
            if _entry_key(name_e) == _entry_key(target_name):
                # 分享条目名已与规范名一致（如「李熊猫」分享 → 目标名也是李熊猫）
                # → 光鸭对同名 rename 会报错，直接视为已达标
                log.info("分享条目名已符合规范命名，无需改名: %s", name_e)
                return True
            if not _rename_verified(client, e["file_id"], target_name, parent_id):
                log.warning("分享产物改名未生效，保持原名: %s", name_e)
                return False
            # 单集文件 → 收进剧名文件夹（一部电视剧一个文件夹）
            if show_dir and ext:
                show_id = _ensure_subdir(client, parent_id, show_dir)
                client.move_file(e["file_id"], show_id)
                inner = [x.get("name") for x in client.list_dir(show_id)]
                if target_name in inner:
                    log.info("分享单集已收进剧名文件夹: %s/%s", show_dir, target_name)
                    return True
                log.warning("分享收纳：移入剧名文件夹后校验失败（%s）", target_name)
                return True  # 改名已生效
            log.info("分享产物已重命名为中文: %s", target_name)
            return True

        # 多条目：整批归拢
        folder_name = show_dir or cn_folder
        sub_id = _ensure_subdir(client, parent_id, folder_name)
        moved = 0
        for e in new_entries:
            if e.get("file_id") == sub_id:
                # 分享根里恰好有与目标同名的文件夹（_ensure_subdir 找到的就是它），
                # move 自己进自己会被光鸭拒绝 → 跳过
                moved += 1
                continue
            try:
                client.move_file(e["file_id"], sub_id)
                moved += 1
            except GuangyaError as exc:
                log.warning("分享收纳：移动 %s 失败（跳过）: %s", e.get("name"), exc)
        log.info("分享合集 %d 条已收进文件夹: %s（成功移动 %d）", len(new_entries), folder_name, moved)
        return moved > 0
    except GuangyaError as exc:
        log.warning("分享收纳失败（保持分享原名）: %s", exc)
        return False


def submit_share_one(client: GuangyaClient, url: str, parent_id: str,
                     cn_title: str = "") -> tuple[bool, str, str, str, bool | None, str]:
    """识别光鸭分享链接并转存到自己网盘，返回结构与 submit_one 对齐。

    (ok, task_id, name, final_status, rename_ok, cn_folder)
    name 在分享场景下没有"英文原名"语义，放转存概要（如 "3 个文件"）。
    """
    parsed = parse_share_url(url)
    if not parsed:
        return False, "", "不是光鸭分享链接", "error", None, ""
    share_id = parsed["share_id"]
    # 发帖标题只作辅助（补年份 / 判断剧集），命名主源是分享链接里的真实文件名
    title = _extract_share_title(cn_title, url) if cn_title else ""
    cn_folder = ""  # 早期失败分支（转存尚未成功）返回空命名，供调用方判空

    try:
        before_ids = {e.get("file_id") for e in client.list_dir(parent_id)}
    except GuangyaError:
        before_ids = set()

    try:
        res = client.save_share(url, parent_id)
    except GuangyaBizError as exc:
        if exc.code == 209:
            msg = "分享需要提取码，链接里没有携带"
        else:
            msg = exc.msg or f"分享转存失败（业务码 {exc.code}）"
        log.warning("分享转存失败 %s: %s", share_id, msg)
        return False, "", msg, "error", None, cn_folder
    except GuangyaError as exc:
        log.warning("分享转存失败 %s: %s", share_id, exc)
        return False, "", str(exc), "error", None, cn_folder

    if not res.get("ok"):
        log.warning("分享转存未完成 %s: %s", share_id, res.get("message"))
        return False, res.get("task_id", ""), res.get("message") or "转存失败", "error", None, ""

    # 命名主源 = 分享链接里的真实文件名（链接内文件名才是片名权威来源）；
    # 发帖标题仅辅助（真实名缺年份时用标题里的年份补全、剧集文件夹判定）。
    entries = res.get("entries") or []
    share_names = [e.get("name", "") for e in entries if e.get("name")]
    primary = share_names[0] if share_names else title
    cn_folder = build_cn_filename(primary) if primary else ""
    # 辅助①：真实名是纯英文（压制组命名，如 Summer.2026.S01E04）/ 无中文，而发帖
    # 标题带中文译名 → 用标题的中文规范名（链接里是英文名，频道标题才是中文译名）。
    # 真实名本身含中文（如「小猪佩奇标准命名 全12季」）则直接用真实名，标题不参与。
    if primary and not re.search(r"[一-鿿]", primary) and title and re.search(r"[一-鿿]", title):
        t_folder = build_cn_filename(title)
        if t_folder and re.search(r"[一-鿿]", t_folder):
            # 真实名常带季数签名（S01-S12 / S02），标题可能没有 → 从真实名补回季数
            sig = re.search(r"\.(s\d{1,2}(?:e\d{1,3})?(?:-s\d{1,2})?)\b", primary, re.I)
            if sig and sig.group(1).lower() not in t_folder.lower():
                # t_folder 以「(年份)」收尾（媒体库格式）时空格分隔，否则沿用点分隔
                sep = " " if t_folder.endswith(")") else "."
                t_folder = f"{t_folder}{sep}{sig.group(1)}"
            cn_folder = t_folder
    # 辅助②：命名缺年份时，拿标题里的年份补上（如「小猪佩奇.S01-S12」+「(2004)」
    # → 小猪佩奇 (2004) S01-S12）；整季范围（X.S01-SNN）则把年份插到片名与范围之间。
    if cn_folder and title:
        m = re.search(r"\(?(\d{4})\)?", title)
        if m and m.group(1) not in cn_folder:
            yr = m.group(1)
            # 单集/单季签名（.SxxExx / .Sxx 词尾）：年份归剧名文件夹，文件名不再塞年份；
            # 仅整季范围（.S01-S12）保留年份（小猪佩奇 (2004) S01-S12）。
            if not re.search(r"\.s0?\d(?:e0?\d)?$", cn_folder, re.I):
                rng = re.match(r"^(.+?)\.(s0?\d-s0?\d)$", cn_folder, re.I)
                cn_folder = (f"{rng.group(1)} ({yr}) {rng.group(2)}" if rng
                             else f"{cn_folder} ({yr})")
    show_dir = ""
    if primary or title:
        try:
            src = title or primary
            cand = show_folder(src)
            info = analyze(src)
            if info.sig and cand and cand != cn_folder:
                show_dir = cand
        except Exception:  # noqa: BLE001 - 剧名文件夹算不出不影响主流程
            show_dir = ""

    # 转存落盘成功 → 中文命名 + 剧集收纳（失败不回滚，只降级为保留分享原名）
    rename_ok: bool | None = None
    if cn_folder:
        rename_ok = _organize_share_entries(client, parent_id, before_ids,
                                            int(res.get("files") or 0),
                                            cn_folder, show_dir=show_dir)
    summary = f"{res.get('files', 0)} 个文件"
    log.info("分享转存完成 %s: %s → %s", share_id, summary, parent_id)
    return True, res.get("task_id", ""), summary, "done", rename_ok, cn_folder


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
        # 与 dedup 判准入时用同一套输入（原始标题 + 规范中文名），
        # 否则会出现「按欧美电影准入、却落到华语电影目录」的两处打架。
        try:
            info = analyze(text)
            extra = info.folder
            region_hint = info.region_hint
            region_hint_strong = info.region_hint_strong
        except Exception:  # noqa: BLE001 - 命名失败不影响分类，退回只用原标题
            extra = ""
            region_hint = ""
            region_hint_strong = False
        cr = classifier.classify(text, extra=extra, region_hint=region_hint,
                                 region_hint_strong=region_hint_strong)
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

            if not store.seen(h):
                store.add(MagnetRecord(hash=h, channel=msg.channel, message_id=msg.message_id,
                                       title=msg.text[:120]))
            target, category = pick_target(msg.text)
            if parse_share_url(url):
                # 光鸭分享链接 → 分享转存链路（转存到自己盘后做中文命名收纳）。
                # 不走 max_retries：分享转存不是幂等操作，重试可能产生重复副本。
                ok2, task_id, name, final_status, rename_ok, cn_folder = submit_share_one(
                    client, url, target, cn_title=msg.text)
            else:
                ok2, task_id, name, final_status, rename_ok, cn_folder = submit_one(
                    client, url, target, max_retries, cn_title=msg.text)
            if ok2:
                db_status = "done" if final_status == "done" else ("upgraded" if is_upgrade else "submitted")
                renamed = 1 if rename_ok is True else (2 if rename_ok is False else 0)
                store.update(h, status=db_status,
                             task_id=task_id, category=category,
                             parent_id=target, cn_folder=cn_folder,
                             renamed=renamed)
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
                # 必须查【全量】任务（含已完成 2 / 失败 3,5）：
                # 实测光鸭按 status 过滤，若只查 [0,1,4]，任务完成的瞬间就会从
                # 结果里消失 → 被下面的「不在列表」分支误标为「任务被清除」，
                # 永远走不到补改名/记账逻辑（这是「落盘仍是英文名」的元凶之一）。
                pending_tasks = client.list_tasks()
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
                        store.update(rec.hash, status="failed", reason="任务被清除（可能手动删除）")
                        updated += 1
                        continue
                    t = task_map[tid]
                    if t.status == GuangyaClient.STATUS_SUCCESS:
                        store.update(rec.hash, status="done")
                        # 提交时超时、实际在后台才完成的任务，落盘成功也要补记账本
                        _record_ledger(store, rec.title or "", rec.category or "")
                        # 提交路径因超时而未改名时，监控线程补做中文改名
                        if rec.cn_folder and rec.parent_id and rec.renamed not in (1, 2):
                            # 剧名文件夹与提交路径同逻辑：从原标题重算（剧集单集需收纳）
                            show_dir = ""
                            try:
                                if rec.title:
                                    cand = show_folder(rec.title)
                                    info = analyze(rec.title)
                                    if info.sig and cand and cand != rec.cn_folder:
                                        show_dir = cand
                            except Exception:  # noqa: BLE001
                                show_dir = ""
                            try:
                                rename_ok = _rename_artifact_to_cn(
                                    client, tid, t.name or "", rec.parent_id, rec.cn_folder,
                                    show_dir=show_dir)
                                store.update(rec.hash, renamed=1 if rename_ok else 2)
                                if rename_ok:
                                    log.info("监控补改名成功: %s", rec.cn_folder)
                                else:
                                    log.warning("监控补改名失败: %s", rec.cn_folder)
                            except Exception as exc:
                                log.warning("监控补改名异常: %s", exc)
                                store.update(rec.hash, renamed=2)
                        updated += 1
                        log.info("任务 %s 已完成", tid)
                    elif t.status in (GuangyaClient.STATUS_FAILED, GuangyaClient.STATUS_FAILED_ALT):
                        store.update(rec.hash, status="failed", reason=t.message or "离线下载失败")
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
                    "支持：磁力 magnet: / 迅雷 thunder: / 电驴 ed2k: / http 直链 / "
                    "光鸭分享链接（guangyapan.com/share/…）。")
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
        proxy=cfg.source.proxy, comments=cfg.source.comments,
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
        source_obj = WebScraper(cfg.source.channels, interval=cfg.source.poll_interval,
                                proxy=cfg.source.proxy,
                                detail_fallback=cfg.source.detail_fallback)

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
