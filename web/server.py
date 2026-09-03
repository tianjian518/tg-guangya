"""TG 频道资源 → 光鸭云盘 自动转存：Web 管理面板（FastAPI）。

启动：
    cd /workspace/tg-guangya
    python web/server.py                 # 默认读同目录 config.yaml
    python web/server.py --config x.yaml # 指定配置
    python web/server.py --port 8080

打开 http://localhost:8000 即可使用。
"""
from __future__ import annotations

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
import threading
import time
import zipfile
from pathlib import Path

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
from core.data_dir import resolve_config_path, get_data_dir, resolve_rel  # noqa: E402

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
    cfg = c
    client = _make_client(cfg, config_path)
    store = Store(cfg.storage_db)


# ---------- 全局状态 ----------
_reload_state(CONFIG_PATH)
_worker_proc: subprocess.Popen | None = None
_login_sessions: dict[str, dict] = {}  # device_code -> {device_code, interval, expires_at}

app = FastAPI(title="TG → 光鸭 自动转存", version="1.0.3")


# ---------- 工具 ----------
def _qr_data_url(url: str) -> str:
    import qrcode

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


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
        "version": "1.0.3",
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
            "version": "1.0.3",
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
    }


@app.put("/api/settings")
def put_settings(body: dict):
    cfg.apply_settings(body)
    cfg.save(str(CONFIG_PATH))
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
def tasks(status: int | None = None):
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
