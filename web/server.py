"""TG 频道资源 → 光鸭云盘 自动转存：Web 管理面板（FastAPI）。

启动：
    cd /workspace/tg-guangya
    python web/server.py                 # 默认读同目录 config.yaml
    python web/server.py --config x.yaml # 指定配置
    python web/server.py --port 8080

打开 http://localhost:8000 即可使用。
"""
from __future__ import annotations

# ---------- 版本号（单一来源，改这一处即可） ----------
__version__ = "1.0.16"

import argparse
import base64
import datetime
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import asyncio
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# 让 core / adapters / main 可被导入
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core.config import AppConfig  # noqa: E402
from core.guangya import GuangyaClient, GuangyaError  # noqa: E402
from core.store import Store  # noqa: E402
from core.data_dir import resolve_config_path, get_data_dir, resolve_rel, REPO_BASE  # noqa: E402
from adapters.userbot import UserbotSource  # noqa: E402
from adapters.web_scraper import WebScraper  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("web")

STATIC_DIR = BASE / "web" / "static"
WORKER_LOG = BASE / "web" / "worker.log"
CONFIG_PATH = resolve_config_path()  # 数据目录里的 config.yaml（/data 或本地 data/）


def _make_client(c: AppConfig, config_path: Path) -> GuangyaClient:
    return GuangyaClient(
        access_token=c.guangya.access_token,
        refresh_token=c.guangya.refresh_token,
        client_id=c.guangya.client_id,
        device_id=c.guangya.device_id,
        on_token_change=lambda a, r: c.save_token(a, r, str(config_path)),
    )


def _apply_data_paths(c: AppConfig) -> None:
    """把配置里的相对路径（库文件/会话文件）收敛到数据目录。"""
    d = get_data_dir()
    c.storage_db = resolve_rel(d, c.storage_db)
    c.telegram.session = resolve_rel(d, c.telegram.session)


def _reload_state(config_path: Path) -> None:
    """重新加载全局配置 / 客户端 / 数据库（首次启动与「还原备份」后共用）。"""
    global cfg, client, store
    c = AppConfig.load(str(config_path))
    _apply_data_paths(c)
    # 先关掉旧库连接，避免反复重载导致 sqlite 连接泄漏
    old = globals().get("store")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    cfg = c
    client = _make_client(cfg, config_path)
    store = Store(cfg.storage_db)


_cfg_mtime = 0.0


def _sync_config_if_changed() -> None:
    """配置被别的进程改过就重新加载。

    面板用子进程拉起 main.py 做监听，而「频道自动发现」是 main.py 里的后台线程，
    它追加频道时只写配置文件、改的是**自己进程**的内存；面板进程的内存配置不会变，
    于是页面上频道数一直停在旧值（看起来像「没自动添加」，其实文件里早写进去了）。
    这里在读配置的接口里比对文件修改时间，变了就重载一次。
    """
    global _cfg_mtime
    try:
        mtime = os.path.getmtime(str(CONFIG_PATH))
    except OSError:
        return
    if _cfg_mtime == 0.0:  # 首次只记录，不触发重载
        _cfg_mtime = mtime
        return
    if mtime == _cfg_mtime:
        return
    _cfg_mtime = mtime
    try:
        _reload_state(CONFIG_PATH)
        log.info("检测到配置文件被更新，已重新加载：频道 %d 个", len(cfg.source.channels))
    except Exception as exc:
        log.warning("配置自动重载失败: %s", exc)


# ---------- 全局状态 ----------
_reload_state(CONFIG_PATH)
_worker_proc: subprocess.Popen | None = None
_login_sessions: dict[str, dict] = {}  # device_code -> {device_code, interval, expires_at}

app = FastAPI(title="TG → 光鸭 自动转存", version=__version__)


# ---------- 工具 ----------
def _qr_data_url(url: str) -> str:
    """生成二维码的 data URL。

    生成失败（如容器缺 Pillow）时返回空串，由前端改用授权链接兜底，
    避免整个扫码登录接口 500 掉。
    """
    try:
        import qrcode

        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        log.warning("生成二维码图片失败（前端将改用授权链接兜底）: %s", exc)
        return ""


def _client_safe() -> GuangyaClient:
    """返回已确保令牌有效的客户端；失败抛 HTTP 401。"""
    try:
        client.ensure_token()
    except GuangyaError as exc:
        raise HTTPException(status_code=401, detail=f"光鸭未登录或令牌失效：{exc}")
    return client


# ---------- 静态资源 ----------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------- 状态 ----------
@app.get("/api/status")
def api_status():
    _sync_config_if_changed()
    logged_in = bool(client.token)
    try:
        stats = store.stats()
    except Exception:
        stats = {}
    return {
        "logged_in": logged_in,
        "account": cfg.guangya.access_token and "已登录" or "未登录",
        "channel_count": len(cfg.source.channels),
        "source_type": cfg.source.type,
        "discovery_enabled": cfg.discovery.enabled,
        "organize_enabled": cfg.organize.enabled,
        "organize_structure": cfg.organize.structure,
        "worker_running": _worker_proc is not None and _worker_proc.poll() is None,
        "stats": stats,
        "data_dir": str(get_data_dir()),
        "version": __version__,
    }


# ---------- 自动分类 ----------
def _organize_payload() -> dict:
    from core.classifier import default_mapping_flat, KIND_NAMES, REGION_NAMES

    og = cfg.organize
    mapping = []
    for item in default_mapping_flat():
        key = f"{item['kind']}:{item['region']}"
        name = og.mapping.get(key) or item["name"]
        mapping.append({**item, "name": name})
    # 用户自己加的、默认表里没有的组合
    for key, name in og.mapping.items():
        k, _, r = key.partition(":")
        if k and r and not any(x["kind"] == k and x["region"] == r for x in mapping):
            mapping.append({
                "name": name, "kind": k, "kind_name": KIND_NAMES.get(k, k),
                "region": r, "region_name": REGION_NAMES.get(r, r),
            })
    return {
        "enabled": og.enabled,
        "structure": og.structure,
        "create_missing": og.create_missing,
        "unknown_dir": og.unknown_dir,
        "mapping": mapping,
        "kinds": [{"value": k, "name": v} for k, v in KIND_NAMES.items()],
        "regions": [{"value": k, "name": v} for k, v in REGION_NAMES.items()],
    }


@app.get("/api/organize")
def get_organize():
    return _organize_payload()


@app.put("/api/organize")
def put_organize(body: dict):
    og = body.get("organize") or body
    mapping = og.get("mapping")
    if isinstance(mapping, list):
        og = dict(og)
        og["mapping"] = {
            f"{str(m.get('kind', '')).strip()}:{str(m.get('region', '')).strip()}": str(m.get("name", "")).strip()
            for m in mapping if m.get("kind") and m.get("region") and m.get("name")
        }
    cfg.apply_settings({"organize": og})
    cfg.save(str(CONFIG_PATH))
    return {"ok": True, "organize": _organize_payload()}


@app.post("/api/organize/preview")
def preview_organize(body: dict):
    """给一条标题，返回分类结果。可带未保存的 structure/mapping 预览效果。"""
    from core.classifier import Classifier

    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="请填标题")
    og = cfg.organize
    mapping = og.mapping or None
    structure = og.structure
    if "structure" in body:
        structure = str(body["structure"]).strip().lower()
    if isinstance(body.get("mapping"), list):
        mapping = {
            f"{str(m.get('kind', '')).strip()}:{str(m.get('region', '')).strip()}": str(m.get("name", "")).strip()
            for m in body["mapping"] if m.get("kind") and m.get("region") and m.get("name")
        }
    elif isinstance(body.get("mapping"), dict):
        mapping = body["mapping"]
    c = Classifier(mapping=mapping, structure=structure, unknown_dir=og.unknown_dir)
    r = c.classify(title, str(body.get("extra") or ""))
    return {
        "category": r.category,
        "kind": r.kind,
        "kind_name": r.kind_name,
        "region": r.region,
        "region_name": r.region_name,
        "confidence": r.confidence,
        "signals": r.signals[:8],
    }


# ---------- 备份 / 还原（仿 OpenList）----------
def _backup_file_list() -> list[Path]:
    """收集要打包进备份的数据文件：配置、去重库、会话、数据目录内的种子文件。"""
    d = get_data_dir()
    files: list[Path] = []
    cfg_path = d / "config.yaml"
    if cfg_path.exists():
        files.append(cfg_path)
    db_path = Path(cfg.storage_db)
    if db_path.exists():
        files.append(db_path)
    sess_path = Path(cfg.telegram.session)
    if sess_path.exists():
        files.append(sess_path)
    # 仅当种子文件落在数据目录内才一并备份（仓库内的种子属于代码，不备份）
    sf = cfg.discovery.seed_file
    if sf:
        sp = Path(sf)
        if not sp.is_absolute():
            sp = d / sp
        if sp.exists() and sp.parent.resolve() == d.resolve():
            files.append(sp)
    return files


@app.get("/api/backup/info")
def backup_info():
    files = _backup_file_list()
    return {
        "data_dir": str(get_data_dir()),
        "files": [f.name for f in files],
        "size": sum(f.stat().st_size for f in files),
    }


@app.get("/api/backup/download")
def backup_download():
    files = _backup_file_list()
    if not files:
        raise HTTPException(status_code=404, detail="没有可备份的数据（配置尚未生成）")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=f.name)
        manifest = {
            "app": "tg-guangya",
            "version": __version__,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "data_dir": str(get_data_dir()),
            "files": [f.name for f in files],
        }
        z.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    buf.seek(0)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=tg-guangya-backup-{ts}.zip"},
    )


@app.post("/api/backup/restore")
async def backup_restore(file: UploadFile = File(...)):
    raw = await file.read()
    # 1) 校验 zip 合法、无路径穿越、必须含 config.yaml
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"不是有效的备份文件（zip）：{exc}")
    names = zf.namelist()
    for n in names:
        if n.startswith("/") or ".." in n.replace("\\", "/").split("/"):
            raise HTTPException(status_code=400, detail="备份文件包含非法路径，已拒绝")
    if "config.yaml" not in names:
        raise HTTPException(status_code=400, detail="备份缺少 config.yaml，无法还原")

    # 2) 先校验待还原的配置能否正常解析（不影响现有数据）
    d = get_data_dir()
    tmp = d / ".restore_tmp.yaml"
    try:
        with zf.open("config.yaml") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        AppConfig.load(str(tmp))
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"备份里的配置解析失败：{exc}")
    finally:
        tmp.unlink(missing_ok=True)

    # 3) 只有 config.yaml / 去重库 / 会话文件允许落地，其余一律丢弃
    allow = {
        "config.yaml",
        Path(cfg.storage_db).name,
        Path(cfg.telegram.session).name,
    }
    safe_names = [os.path.basename(n) for n in names if os.path.basename(n) in allow]

    # 4) 先停 worker、备份现有数据，再落地
    was_running = _worker_proc is not None and _worker_proc.poll() is None
    _stop_worker()
    try:
        store.close()
    except Exception:
        pass
    for keep in ("config.yaml", Path(cfg.storage_db).name, Path(cfg.telegram.session).name):
        src = d / keep
        if src.exists():
            try:
                shutil.copyfile(src, d / (keep + ".bak"))
            except Exception as exc:
                log.warning("备份现有 %s 失败: %s", keep, exc)

    restored = []
    for n in safe_names:
        target = d / n
        with zf.open(n) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        restored.append(n)

    # 5) 重载全局状态（配置/客户端/数据库），按需重启 worker
    _reload_state(CONFIG_PATH)
    if was_running and client.token:
        try:
            _start_worker()
        except Exception as exc:
            log.warning("还原后重启监听失败: %s", exc)

    return {"ok": True, "restored": restored}


# ---------- 设置 ----------
@app.get("/api/settings")
def get_settings():
    _sync_config_if_changed()
    return {
        "sources": {
            "type": cfg.source.type,
            "channels": list(cfg.source.channels),
            "poll_interval": cfg.source.poll_interval,
        },
        "filter": {
            "include_keywords": list(cfg.filter.include_keywords),
            "exclude_keywords": list(cfg.filter.exclude_keywords),
            "min_resolution": cfg.filter.min_resolution,
        },
        "output": {
            "parent_id": cfg.output.parent_id,
            "save_path": cfg.output.save_path,
        },
        "discovery": {
            "enabled": cfg.discovery.enabled,
            "interval_hours": cfg.discovery.interval_hours,
            "seed_urls": list(cfg.discovery.seed_urls),
            "seed_file": cfg.discovery.seed_file,
        },
        "storage_db": cfg.storage_db,
        "notify_console": cfg.notify_console,
        "max_retries": cfg.max_retries,
        "scan_history": cfg.scan_history,
        "history_pages": cfg.history_pages,
        "organize": {
            "enabled": cfg.organize.enabled,
            "structure": cfg.organize.structure,
            "create_missing": cfg.organize.create_missing,
            "unknown_dir": cfg.organize.unknown_dir,
            "mapping": dict(cfg.organize.mapping),
        },
        "dedup": {
            "cloud_check_new": cfg.dedup.cloud_check_new,
            "cache_ttl": cfg.dedup.cache_ttl,
            "upgrade": cfg.dedup.upgrade,
        },
        "telegram": {
            "api_id": cfg.telegram.api_id,
            "api_hash": cfg.telegram.api_hash,
            "session": cfg.telegram.session,
        },
        "bot": {
            "enabled": cfg.bot.enabled,
            "token": cfg.bot.token,
            "admin_ids": list(cfg.bot.admin_ids),
            "notify": cfg.bot.notify,
            "proxy": cfg.bot.proxy,
            "allow_anyone": cfg.bot.allow_anyone,
        },
    }


@app.put("/api/settings")
def put_settings(body: dict):
    cfg.apply_settings(body)
    cfg.save(str(CONFIG_PATH))
    return {"ok": True}


@app.post("/api/channels/prune")
def prune_channels():
    """一键清理：移除「最近 N 页内从未出现过资源链接」的频道（纯噪音频道）。

    这些频道大多是自动发现从聚合页扒出来的名字，跟影视资源无关。
    会写回配置文件。抓取失败 / 抓取异常的频道一律不动，避免误删。
    """
    _sync_config_if_changed()
    channels = list(cfg.source.channels)
    if not channels:
        return {"removed": [], "kept": 0, "total": 0}
    pages = max(2, int(cfg.history_pages))
    sc = WebScraper(channels, interval=cfg.source.poll_interval, proxy=cfg.source.proxy)
    zero: list[str] = []
    for ch in channels:
        try:
            msgs = list(sc.iter_history(ch, pages))
            if not msgs:
                continue  # 抓不到的不动
            if sum(len(m.links) for m in msgs) == 0:
                zero.append(ch)
        except Exception:
            continue  # 抓取异常的不动
    if zero:
        drop = {z.lower() for z in zero}
        cfg.source.channels = [c for c in channels if str(c).strip().lower() not in drop]
        cfg.save(str(CONFIG_PATH))
    return {"removed": zero, "kept": len(cfg.source.channels), "total": len(channels)}


@app.post("/api/channels/reset")
def reset_channels_to_curated():
    """一键恢复为仓库内置的精选影视频道（28 个已实测出片的频道），清掉自动发现扒来的噪音频道。

    会写回配置文件（/data/config.yaml）。调用后需「停止监听 → 启动监听」让新列表生效。
    """
    example = REPO_BASE / "config.example.yaml"
    if not example.exists():
        raise HTTPException(status_code=500, detail="找不到示例配置 config.example.yaml")
    ex = AppConfig.load(str(example))
    curated = list(ex.source.channels or [])
    if not curated:
        raise HTTPException(status_code=500, detail="示例配置里没有精选频道")
    cfg.source.channels = curated
    cfg.save(str(CONFIG_PATH))
    return {"channels": curated, "total": len(curated)}


# ---------- Telegram Userbot 登录（网页流程：手机 + 验证码）----------
# 登录走后台常驻事件循环：Telethon 的异步方法必须在同一个 loop 上跑，
# 而 HTTP 请求是分多次的（发码 → 填码 → 可能填 2FA 密码），所以用一个
# 守护线程里的 event loop 来承载，避免「每次 asyncio.run 都新建 loop」导致的冲突。
_userbot_pending: dict = {}
_userbot_loop = None
_userbot_loop_thread = None


def _ub_loop():
    global _userbot_loop, _userbot_loop_thread
    if _userbot_loop is None or _userbot_loop.is_closed():
        _userbot_loop = asyncio.new_event_loop()
        _userbot_loop_thread = threading.Thread(target=_userbot_loop.run_forever, daemon=True)
        _userbot_loop_thread.start()
    return _userbot_loop


def _ub_run(coro):
    return asyncio.run_coroutine_threadsafe(coro, _ub_loop()).result()


def _build_userbot_source() -> UserbotSource:
    return UserbotSource(
        cfg.telegram.api_id, cfg.telegram.api_hash,
        cfg.telegram.session, list(cfg.source.channels), proxy=cfg.source.proxy,
    )


@app.get("/api/userbot/status")
def userbot_status():
    if not cfg.telegram.api_id or not cfg.telegram.api_hash:
        return {"logged_in": False, "error": "请先在设置里填写 api_id / api_hash"}
    src = _build_userbot_source()
    try:
        authed = _ub_run(src.is_authorized())
    except Exception as exc:
        return {"logged_in": False, "error": str(exc)}
    return {"logged_in": bool(authed)}


@app.post("/api/userbot/login/start")
def userbot_login_start(body: dict):
    phone = str(body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="请填写手机号（含国家区号，如 +8613800138000）")
    if not cfg.telegram.api_id or not cfg.telegram.api_hash:
        raise HTTPException(status_code=400, detail="请先在「系统设置 → Telegram 账号」填写 api_id / api_hash")
    src = _build_userbot_source()
    try:
        _ub_run(src.connect())
        sent = _ub_run(src.send_code(phone))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"发送验证码失败：{exc}")
    _userbot_pending[cfg.telegram.session] = {
        "phone": phone, "phone_code_hash": sent.phone_code_hash, "src": src,
    }
    return {"status": "code_sent", "phone": phone}


@app.post("/api/userbot/login/code")
def userbot_login_code(body: dict):
    from telethon.errors import SessionPasswordNeededError
    code = str(body.get("code") or "").strip()
    pend = _userbot_pending.get(cfg.telegram.session)
    if not pend:
        raise HTTPException(status_code=400, detail="登录会话已失效，请重新点击登录")
    src = pend["src"]
    try:
        _ub_run(src.sign_in_code(pend["phone"], code, pend["phone_code_hash"]))
    except SessionPasswordNeededError:
        return {"status": "password_needed"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"验证码错误：{exc}")
    _finalize_userbot_login(src)
    return {"status": "success"}


@app.post("/api/userbot/login/password")
def userbot_login_password(body: dict):
    pwd = str(body.get("password") or "").strip()
    pend = _userbot_pending.get(cfg.telegram.session)
    if not pend:
        raise HTTPException(status_code=400, detail="登录会话已失效，请重新点击登录")
    src = pend["src"]
    try:
        _ub_run(src.sign_in_password(pwd))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"密码错误：{exc}")
    _finalize_userbot_login(src)
    return {"status": "success"}


def _finalize_userbot_login(src: UserbotSource) -> None:
    # Telethon 在 sign_in 成功并 disconnect 时会把登录态写入 session 文件
    try:
        _ub_run(src.disconnect())
    except Exception:
        pass
    _userbot_pending.pop(cfg.telegram.session, None)


@app.post("/api/userbot/logout")
def userbot_logout():
    try:
        p = Path(cfg.telegram.session)
        if p.exists():
            p.unlink()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"退出失败：{exc}")
    return {"ok": True}


# ---------- 监控历史（频道命中 / 转存 / 跳过 全记录）----------
def _fmt_time(ts: float) -> str:
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _record_to_dict(r) -> dict:
    return {
        "hash": r.hash,
        "channel": r.channel,
        "title": r.title,
        "status": r.status,
        "task_id": r.task_id,
        "reason": r.reason,
        "category": r.category,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "updated_text": _fmt_time(r.updated_at),
    }


@app.get("/api/history")
def api_history(limit: int = 50, status: str = ""):
    try:
        rows = store.history(limit=max(1, min(int(limit), 500)), status=status or None)
    except Exception:
        rows = []
    return {"items": [_record_to_dict(r) for r in rows], "total": len(rows)}


# ---------- 频道管理 ----------
@app.get("/api/channels")
def list_channels():
    _sync_config_if_changed()
    return {"channels": list(cfg.source.channels)}


@app.post("/api/channels")
def add_channel(body: dict):
    name = str(body.get("name") or "").strip().lstrip("@")
    if not name:
        raise HTTPException(status_code=400, detail="频道名不能为空")
    added = cfg.add_channels([name], str(CONFIG_PATH))
    return {"ok": True, "added": added, "channels": list(cfg.source.channels)}


@app.delete("/api/channels/{name}")
def delete_channel(name: str):
    name = name.strip().lstrip("@").lower()
    existing = [c for c in cfg.source.channels if c.strip().lstrip("@").lower() != name]
    if len(existing) == len(cfg.source.channels):
        raise HTTPException(status_code=404, detail="频道不存在")
    # 写回：复用 add_channels 的反向——直接改配置
    import yaml

    with open(str(CONFIG_PATH), "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw.setdefault("sources", {})
    raw["sources"]["channels"] = existing
    with open(str(CONFIG_PATH), "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)
    cfg.source.channels = existing
    return {"ok": True, "channels": list(cfg.source.channels)}


# ---------- 光鸭登录 ----------
@app.post("/api/guangya/login/start")
def login_start():
    try:
        info = client.start_qr_login(qr_path=str(BASE / "web" / "login_qr.png"))
    except GuangyaError as exc:
        log.error("扫码登录启动失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    qr = _qr_data_url(info["qr_url"])
    _login_sessions[info["device_code"]] = {
        "device_code": info["device_code"],
        "interval": info["interval"],
        "expires_at": time.time() + info["expires_in"],
    }
    return {
        "qr_data_url": qr,
        "qr_url": info["qr_url"],
        "device_code": info["device_code"],
        "interval": info["interval"],
        "expires_in": info["expires_in"],
    }


@app.get("/api/guangya/login/poll")
def login_poll(device_code: str):
    sess = _login_sessions.get(device_code)
    if not sess:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期，请重新发起")
    if time.time() > sess["expires_at"]:
        _login_sessions.pop(device_code, None)
        return {"status": "expired"}
    status = client.poll_qr_login(device_code, interval=1, timeout=max(2, sess["interval"]))
    if status == "success":
        _login_sessions.pop(device_code, None)
        return {"status": "success"}
    if status in ("denied", "expired"):
        _login_sessions.pop(device_code, None)
    return {"status": status}


@app.post("/api/guangya/logout")
def logout():
    global client
    # 清掉配置里的令牌
    cfg.guangya.access_token = ""
    cfg.guangya.refresh_token = ""
    cfg.save_token("", "", str(CONFIG_PATH))
    # 重建客户端（无令牌）
    client = _make_client(cfg, CONFIG_PATH)
    return {"ok": True}


@app.post("/api/guangya/login/manual")
def login_manual(body: dict):
    """手动填入光鸭令牌（access_token / refresh_token）直接登录。

    用途：当扫码/设备码流程不便使用时，可把在 LitePan 等工具里已经拿到的
    光鸭令牌直接粘进来，跳过扫码。令牌会写入 config.yaml 并立即生效。
    """
    access = str(body.get("access_token") or "").strip()
    refresh = str(body.get("refresh_token") or "").strip()
    if not access and not refresh:
        raise HTTPException(status_code=400, detail="访问令牌与刷新令牌至少要填一个")
    tmp = GuangyaClient(
        access_token=access, refresh_token=refresh,
        client_id=cfg.guangya.client_id, device_id=cfg.guangya.device_id,
    )
    # access_token 由光鸭签发、有效期只有 2 小时，从别的工具里粘过来的往往早已过期
    # （服务端返回 401 token expiry）。所以只要带了 refresh_token，就先换发一次新令牌
    # 再校验——刷新成功即视为登录成功，不必要求 access 本身还有效。
    if refresh:
        try:
            tmp.refresh()
        except GuangyaError as exc:
            log.error("用 refresh_token 换发新令牌失败: %s", exc)
            raise HTTPException(
                status_code=401,
                detail=f"令牌校验失败：{exc}（刷新令牌可能已失效，请重新扫码登录）",
            )
    try:
        info = tmp.me()
    except GuangyaError as exc:
        log.error("令牌校验（/v1/user/me）失败: %s", exc)
        raise HTTPException(status_code=401, detail=f"令牌校验失败：{exc}")
    # 写入的是刷新后的令牌（access 已换新，refresh 也可能被服务端轮换）
    cfg.guangya.access_token = tmp.token or access
    cfg.guangya.refresh_token = tmp.refresh_value or refresh
    cfg.save_token(cfg.guangya.access_token, cfg.guangya.refresh_token, str(CONFIG_PATH))
    global client
    client = tmp
    return {"ok": True, "account": info}


@app.get("/api/guangya/account")
def account():
    c = _client_safe()
    try:
        info = c.me()
    except GuangyaError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"account": info}


@app.get("/api/guangya/folders")
def folders(parent_id: str = ""):
    c = _client_safe()
    try:
        items = c.list_folders(parent_id)
    except GuangyaError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"parent_id": parent_id, "folders": items}


# ---------- 转存任务（实时读取光鸭）----------
@app.get("/api/tasks")
def tasks(status: Optional[int] = None):
    c = _client_safe()
    try:
        items = c.list_tasks(statuses=[status] if status is not None else None)
    except GuangyaError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "tasks": [
            {
                "task_id": t.task_id,
                "name": t.name,
                "size": t.size,
                "status": t.status,
                "status_text": {
                    0: "等待处理", 1: "离线下载中", 2: "已完成", 3: "失败", 4: "重试中", 5: "失败"
                }.get(t.status, "未知"),
                "progress": t.progress,
                "message": t.message,
            }
            for t in items
        ]
    }


@app.post("/api/tasks/cleanup")
def cleanup_tasks():
    c = _client_safe()
    try:
        items = c.list_tasks()
        done = [t.task_id for t in items if t.finished]
        if done:
            c.delete_tasks(done)
    except GuangyaError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "cleaned": len(done)}


# ---------- 频道自动发现（一次性）----------
@app.post("/api/discover/run")
def discover_run():
    from core.discovery import ChannelDiscovery

    seed_file = cfg.discovery.seed_file
    if seed_file and not os.path.isabs(seed_file):
        seed_file = str(BASE / seed_file)
    disc = ChannelDiscovery(
        seed_urls=cfg.discovery.seed_urls,
        seed_file=seed_file,
        interval_hours=cfg.discovery.interval_hours,
    )
    disc.load_known(cfg.source.channels)
    new = disc.discover_once()
    added = cfg.add_channels(sorted(new), str(CONFIG_PATH)) if new else 0
    return {"ok": True, "found": len(new), "added": added, "channels": list(cfg.source.channels)}


# ---------- 运行日志 ----------
@app.get("/api/logs")
def logs(lines: int = 200):
    if not WORKER_LOG.exists():
        return {"logs": []}
    try:
        with open(WORKER_LOG, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read().splitlines()
        return {"logs": data[-lines:]}
    except Exception:
        return {"logs": []}


# ---------- 运行控制（启动/停止监听 worker）----------
def _start_worker() -> bool:
    global _worker_proc
    if _worker_proc and _worker_proc.poll() is None:
        return False  # 已在运行
    if not client.token:
        raise HTTPException(status_code=401, detail="请先登录光鸭账号再启动监听")
    logf = open(WORKER_LOG, "ab", buffering=0)
    env = dict(os.environ)
    _worker_proc = subprocess.Popen(
        [sys.executable, str(BASE / "main.py"), "--config", str(CONFIG_PATH)],
        cwd=str(BASE), stdout=logf, stderr=logf, env=env,
    )
    return True


def _stop_worker() -> bool:
    global _worker_proc
    if not _worker_proc or _worker_proc.poll() is not None:
        return False
    _worker_proc.terminate()
    try:
        _worker_proc.wait(timeout=10)
    except Exception:
        _worker_proc.kill()
    return True


@app.post("/api/worker/start")
def worker_start():
    try:
        started = _start_worker()
    except HTTPException as exc:
        raise exc
    return {"ok": True, "started": started, "running": _worker_proc is not None and _worker_proc.poll() is None}


@app.post("/api/worker/stop")
def worker_stop():
    stopped = _stop_worker()
    return {"ok": True, "stopped": stopped}


@app.get("/api/worker")
def worker_status():
    running = _worker_proc is not None and _worker_proc.poll() is None
    return {"running": running}


# ---------- 挂载静态目录（放在最后，避免覆盖 API 路由）----------
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main():
    ap = argparse.ArgumentParser(description="TG → 光鸭 管理面板")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="配置文件路径")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址")
    ap.add_argument("--port", type=int, default=8000, help="监听端口")
    args = ap.parse_args()
    config_path = Path(args.config)
    globals()["CONFIG_PATH"] = config_path
    _reload_state(config_path)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
