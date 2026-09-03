"""光鸭云盘 API 封装。

注意：光鸭没有公开 API 文档，本模块依据 LitePan 开源项目（PolyForm Noncommercial）
对光鸭驱动的逆向实现编写，接口细节可能随官方变动，请以实际返回为准。

关键接口（来自 LitePan drivers/Guangya）：
  账号域 account.guangyapan.com
    POST /v1/auth/token                       刷新 access_token（有效期仅 2 小时）
  业务域 api.guangyapan.com
    POST /cloudcollection/v1/resolve_res      解析资源 → resType + 文件名
    POST /cloudcollection/v1/create_task      创建离线下载任务
    POST /cloudcollection/v1/list_task        查询任务进度
    POST /cloudcollection/v2/delete_task      删除任务
"""
from __future__ import annotations

import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

ACCOUNT_BASE = "https://account.guangyapan.com"
API_BASE = "https://api.guangyapan.com"
WEB_BASE = "https://www.guangyapan.com"
DEFAULT_CLIENT_ID = "aMe-8VSlkrbQXpUR"  # LitePan 中硬编码的公开 client_id

# 离线任务状态码（来自 LitePan mapOfflineTaskUpdate）
STATUS_PENDING = 0
STATUS_RUNNING = 1
STATUS_SUCCESS = 2
STATUS_FAILED = 3
STATUS_RETRYING = 4
STATUS_FAILED_ALT = 5

STATUS_TEXT = {
    STATUS_PENDING: "等待处理",
    STATUS_RUNNING: "离线下载中",
    STATUS_SUCCESS: "已完成",
    STATUS_FAILED: "失败",
    STATUS_RETRYING: "重试中",
    STATUS_FAILED_ALT: "失败",
}


class GuangyaError(Exception):
    """光鸭接口错误。"""


class AuthExpired(GuangyaError):
    """令牌失效，需要重新扫码登录。"""


@dataclass
class OfflineTask:
    task_id: str
    file_id: str = ""
    name: str = ""
    size: int = 0
    status: int = STATUS_PENDING
    progress: int = 0
    message: str = ""

    @property
    def finished(self) -> bool:
        return self.status in (STATUS_SUCCESS, STATUS_FAILED, STATUS_FAILED_ALT)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


class GuangyaClient:
    """光鸭云盘客户端，自动管理令牌刷新。"""

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        client_id: str = DEFAULT_CLIENT_ID,
        device_id: str = "",
        on_token_change=None,
        timeout: int = 30,
    ) -> None:
        self._access = (access_token or "").strip()
        self._refresh = (refresh_token or "").strip()
        self.client_id = client_id or DEFAULT_CLIENT_ID
        self.device_id = (device_id or "").strip().lower() or uuid.uuid4().hex
        self.on_token_change = on_token_change
        self.timeout = timeout
        self._expire_at = 0.0  # 令牌过期时间戳，0 表示未知
        self._session = requests.Session()

    # ---------- 令牌 ----------

    @property
    def token(self) -> str:
        return self._access

    @property
    def refresh_value(self) -> str:
        """当前生效的 refresh_token（刷新后可能被服务端轮换）。"""
        return self._refresh

    def _persist(self) -> None:
        if self.on_token_change:
            try:
                self.on_token_change(self._access, self._refresh)
            except Exception as exc:  # 持久化失败不应中断流程
                log.warning("保存令牌失败: %s", exc)

    def refresh(self) -> str:
        """用 refresh_token 换取新的 access_token。"""
        if not self._refresh:
            raise AuthExpired("缺少 refresh_token，请重新扫码登录")
        payload = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh,
        }
        data = self._account_post("/v1/auth/token", payload, auth=False)
        access = (data.get("access_token") or "").strip()
        if not access:
            raise AuthExpired("光鸭刷新令牌失败，请重新扫码登录")
        self._access = access
        if (data.get("refresh_token") or "").strip():
            self._refresh = data["refresh_token"].strip()
        # 官方有效期 2 小时，留 15 分钟余量
        self._expire_at = time.time() + 7200 - 900
        self._persist()
        log.info("光鸭令牌已刷新")
        return self._access

    def ensure_token(self) -> str:
        """必要时自动续期。"""
        if not self._access or time.time() >= self._expire_at:
            self.refresh()
        return self._access

    # ---------- 首次扫码登录 ----------

    def start_qr_login(self, qr_path: str = "guangya_login.png") -> dict:
        """生成设备码 + 二维码，供光鸭 App 扫码授权。"""
        data = self._account_post(
            "/v1/auth/device/code", {"client_id": self.client_id, "scope": "user"}, auth=False
        ) or {}
        device_code = (data.get("device_code") or "").strip()
        qr_url = (data.get("verification_uri_complete") or data.get("verification_url") or "").strip()
        interval = int(data.get("interval") or 5)
        expires_in = int(data.get("expires_in") or 120)
        if not device_code or not qr_url:
            raise GuangyaError(
                "光鸭设备码接口返回不完整，无法生成二维码；接口返回: " + str(data)[:300]
            )
        try:
            import qrcode

            qrcode.make(qr_url).save(qr_path)
            log.info("二维码已保存: %s", qr_url and qr_path)
        except Exception as exc:  # 二维码生成失败也能用链接兜底
            log.warning("生成二维码图片失败（可手动打开链接）: %s", exc)
        return {
            "device_code": device_code,
            "qr_url": qr_url,
            "qr_path": qr_path,
            "interval": interval,
            "expires_in": expires_in,
        }

    def poll_qr_login(self, device_code: str, interval: int = 5, timeout: int = 120) -> str:
        """轮询扫码结果，成功则自动写入令牌。返回 success/expired/denied/timeout。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self._session.post(
                    ACCOUNT_BASE + "/v1/auth/token",
                    json={
                        "client_id": self.client_id,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                    },
                    headers=self._api_headers(auth=False),
                    timeout=self.timeout,
                )
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
            except requests.RequestException as exc:
                log.warning("扫码轮询网络异常: %s", exc)
                time.sleep(interval)
                continue
            access = (body.get("access_token") or "").strip()
            if access:
                self._access = access
                if (body.get("refresh_token") or "").strip():
                    self._refresh = body["refresh_token"].strip()
                self._expire_at = time.time() + 7200 - 900
                self._persist()
                return "success"
            err = str(body.get("error") or resp.text or "").lower()
            if "expired" in err:
                return "expired"
            if "denied" in err or "取消" in err or "拒绝" in err or "accessdenied" in err:
                return "denied"
            time.sleep(interval)
        return "timeout"

    def login_interactive(self, qr_path: str = "guangya_login.png", timeout: int = 180) -> tuple[str, str]:
        """交互式扫码登录：生成二维码 → 轮询 → 返回 (access, refresh)。"""
        info = self.start_qr_login(qr_path)
        print("\n请用光鸭云盘 App 扫码登录：")
        print(f"  - 二维码图片: {info['qr_path']}")
        print(f"  - 或浏览器打开: {info['qr_url']}")
        print("  - 等待扫码确认...\n")
        status = self.poll_qr_login(info["device_code"], info["interval"], timeout)
        if status != "success":
            raise AuthExpired(f"扫码登录失败（{status}），请重试")
        print("✅ 登录成功，令牌已保存")
        return self._access, self._refresh

    # ---------- 底层请求 ----------

    def _api_headers(self, auth: bool = True) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "did": self.device_id,
            "dt": "4",
            "Origin": WEB_BASE,
            "Referer": WEB_BASE + "/",
        }
        if auth and self._access:
            headers["Authorization"] = "Bearer " + self._access
        return headers

    def build_account_headers(self) -> dict:
        """账户域（登录/设备码/刷新令牌）专用请求头。

        与 LitePan drivers/Guangya transport.go 的 buildAccountHeaders 对齐：
        光鸭账户接口依赖 X-Device-Sign / X-Device-Id 等客户端指纹头，缺少会导致
        设备码接口返回空 data（表现就是「返回不完整，无法生成二维码」）。
        """
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-Device-Model": "chrome%2F147.0.0.0",
            "X-Device-Name": "PC-Chrome",
            "X-Device-Sign": "wdi10." + self.device_id + "x" * 32,
            "X-Net-Work-Type": "NONE",
            "X-OS-Version": "MacIntel",
            "X-Platform-Version": "1",
            "X-Protocol-Version": "301",
            "X-Provider-Name": "NONE",
            "X-SDK-Version": "9.0.2",
            "X-Client-Id": self.client_id,
            "X-Client-Version": "0.0.1",
            "X-Device-Id": self.device_id,
        }

    def _post(self, base: str, path: str, body: dict, auth: bool = True,
              retry: bool = True, headers: dict | None = None, raw: bool = False) -> Any:
        url = base + path
        req_headers = dict(headers) if headers is not None else self._api_headers(auth)
        resp = self._session.post(
            url, json=body, headers=req_headers, timeout=self.timeout
        )
        if resp.status_code in (401, 403) and auth and retry and self._refresh:
            self.refresh()  # 令牌过期，刷一次重试
            return self._post(base, path, body, auth=auth, retry=False, headers=self._api_headers(auth))
        if resp.status_code >= 400:
            raise GuangyaError(f"光鸭 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            envelope = resp.json()
        except ValueError as exc:
            raise GuangyaError(f"光鸭返回非 JSON: {resp.text[:200]}") from exc
        if raw:
            # 账户接口（设备码 / 登录 / 刷新令牌 / me）直接返回内容，没有 success/data 信封；
            # 之前错误地拆了 data 层导致 device_code 拿不到、报「返回不完整」。
            return envelope
        # 业务接口信封：{code, msg, data:{...}}（部分接口也带 success 字段）
        code = envelope.get("code")
        if isinstance(code, int) and code != 0:
            raise GuangyaError(envelope.get("msg") or envelope.get("message") or f"光鸭业务错误 {code}")
        if envelope.get("success") is False:
            raise GuangyaError(envelope.get("message") or f"光鸭错误 {envelope.get('code')}")
        return envelope.get("data")

    def _get(self, base: str, path: str, headers: dict | None = None,
             params: dict | None = None, raw: bool = False) -> Any:
        """账户域的 GET 请求。

        光鸭部分账户接口（如 /v1/user/me）只接受 GET：POST 会被网关在鉴权之前
        直接返回 501 Method Not Allowed，表面看像「令牌错误」，实为方法错误。
        """
        url = base + path
        req_headers = dict(headers) if headers is not None else self._api_headers(True)
        resp = self._session.get(url, headers=req_headers, params=params, timeout=self.timeout)
        if resp.status_code >= 400:
            raise GuangyaError(f"光鸭 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            envelope = resp.json()
        except ValueError as exc:
            raise GuangyaError(f"光鸭返回非 JSON: {resp.text[:200]}") from exc
        if raw:
            return envelope
        code = envelope.get("code")
        if isinstance(code, int) and code != 0:
            raise GuangyaError(envelope.get("msg") or envelope.get("message") or f"光鸭业务错误 {code}")
        if envelope.get("success") is False:
            raise GuangyaError(envelope.get("message") or f"光鸭错误 {envelope.get('code')}")
        return envelope.get("data")

    def _account_post(self, path: str, body: dict, auth: bool = False) -> Any:
        # raw=True：账户接口返回顶层 JSON，不做 data 拆包
        return self._post(ACCOUNT_BASE, path, body, auth=False,
                          headers=self.build_account_headers(), raw=True)

    def _api_post(self, path: str, body: dict) -> Any:
        self.ensure_token()
        return self._post(API_BASE, path, body, auth=True, raw=False)

    # ---------- 业务接口 ----------

    def resolve(self, url: str) -> dict:
        """解析磁力/链接，返回 resType 与文件名。"""
        data = self._api_post("/cloudcollection/v1/resolve_res", {"url": url})
        data = data or {}
        info = {}
        for key in ("urlResInfo", "emuleResInfo", "btResInfo", "torrentResInfo"):
            part = data.get(key) or {}
            if (part.get("fileName") or "").strip():
                info["name"] = part["fileName"].strip()
                break
        info["res_type"] = data.get("resType", 0)
        return info

    def create_offline_task(self, url: str, parent_id: str = "") -> tuple[str, str]:
        """提交离线下载，返回 (task_id, 解析出的名称)。"""
        resolved = self.resolve(url)
        body = {
            "url": url,
            "parentId": parent_id or "",
            "resType": resolved.get("res_type", 0),
        }
        data = self._api_post("/cloudcollection/v1/create_task", body) or {}
        task_id = (data.get("taskId") or "").strip()
        if not task_id:
            raise GuangyaError("光鸭未返回 taskId")
        return task_id, resolved.get("name", "")

    def list_tasks(self, statuses: Iterable[int] | None = None, page_size: int = 50) -> list[OfflineTask]:
        """拉取离线任务列表（自动翻页）。"""
        body: dict[str, Any] = {
            "pageSize": page_size,
            "status": list(statuses) if statuses else list(range(6)),
        }
        out: list[OfflineTask] = []
        cursor = ""
        while True:
            if cursor:
                body["cursor"] = cursor
            data = self._api_post("/cloudcollection/v1/list_task", body) or {}
            for item in data.get("list") or []:
                out.append(
                    OfflineTask(
                        task_id=(item.get("taskId") or "").strip(),
                        file_id=(item.get("fileId") or "").strip(),
                        name=(item.get("fileName") or "").strip(),
                        size=int(item.get("fileSize") or 0),
                        status=int(item.get("status") or 0),
                        progress=int(float(item.get("progress") or 0)),
                        message=(item.get("errorMessage") or item.get("message") or "").strip(),
                    )
                )
            if not data.get("hasMore"):
                break
            nxt = (data.get("cursor") or "").strip()
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    def delete_tasks(self, task_ids: list[str]) -> None:
        """删除离线任务记录。"""
        if not task_ids:
            return
        self._api_post("/cloudcollection/v2/delete_task", {"taskIds": task_ids})

    def get_task(self, task_id: str) -> OfflineTask | None:
        for task in self.list_tasks():
            if task.task_id == task_id:
                return task
        return None

    # ---------- 目录浏览（用于设置转存目录）----------

    def list_folders(self, parent_id: str = "", page_size: int = 200) -> list[dict]:
        """列出某目录下的文件夹（仅目录），用于设置转存目录。

        接口来自 LitePan drivers/Guangya transport.go: pathFileList
          POST /userres/v1/file/get_file_list
        resType == 2 表示文件夹（见 models.go fileEntry.toFileItem）。

        注意：光鸭的 page 从 **0** 开始计数（传 1 会越过首页，只返回 total 而没有
        list，表现为「盘里空空如也」，进而导致去重失效、分类目录被重复创建）。
        """
        body = {
            "parentId": parent_id or "",
            "page": 0,
            "pageSize": page_size,
            "orderBy": 1,
            "sortType": 0,
        }
        data = self._api_post("/userres/v1/file/get_file_list", body) or {}
        out: list[dict] = []
        for e in data.get("list") or []:
            if int(e.get("resType") or 0) == 2:
                out.append({
                    "file_id": (e.get("fileId") or "").strip(),
                    "name": (e.get("fileName") or "").strip(),
                    "parent_id": (e.get("parentId") or "").strip(),
                })
        return out

    def create_folder(self, parent_id: str = "", name: str = "") -> str:
        """在指定目录下新建文件夹，返回新目录的 fileId。

        接口来自 LitePan drivers/Guangya：
          transport.go  pathCreateDir = "/userres/v1/file/create_dir"
          ops.go        CreateFolder(ctx, parentID, name) -> body {"parentId", "dirName"}
                        返回 data: {fileId, fileName, resType, ctime, utime}
        """
        name = (name or "").strip()
        if not name:
            raise GuangyaError("文件夹名称不能为空")
        data = self._api_post(
            "/userres/v1/file/create_dir", {"parentId": parent_id or "", "dirName": name}
        ) or {}
        file_id = (data.get("fileId") or "").strip()
        if not file_id:
            raise GuangyaError(f"光鸭建目录未返回 fileId: {name}")
        log.info("已在光鸭创建目录: %s", name)
        return file_id

    def me(self) -> dict:
        """获取当前登录的账号信息（昵称、手机号等）。

        注意：该端点只接受 **GET**。此前用 POST 会被网关在鉴权之前直接拒绝，
        返回 501 `{"error":"unimplemented","error_code":12,"Method Not Allowed"}`，
        表现为「令牌校验失败」，实际与令牌无关。
        """
        h = self.build_account_headers()
        if self._access:
            h["Authorization"] = "Bearer " + self._access
        return self._get(ACCOUNT_BASE, "/v1/user/me", headers=h, raw=True) or {}

    def delete_file(self, parent_id: str, file_id: str) -> None:
        """删除网盘里的文件/目录（用于洗版时替换旧版本）。

        接口来自 LitePan drivers/Guangya 的 file 删除类操作，body 形如
        {"parentId": "...", "fileIds": ["..."]}。删除为不可逆操作，调用前请确保
        file_id 确实指向待替换的旧版本。失败时抛 GuangyaError。
        """
        if not file_id:
            raise GuangyaError("删除文件缺少 fileId")
        self._api_post(
            "/userres/v1/file/delete",
            {"parentId": parent_id or "", "fileIds": [file_id]},
        )
        log.info("已删除光鸭文件: %s", file_id)

    def list_dir(self, parent_id: str = "", page_size: int = 200) -> list[dict]:
        """列出某目录下的全部条目（文件 + 文件夹，自动翻页）。

        与 list_folders 的区别：不过滤 resType，返回文件与目录，供云端查重
        时按文件名匹配使用。接口同 LitePan transport.go: pathFileList
          POST /userres/v1/file/get_file_list
        返回字段见 models.go fileEntry（fileName / fileSize / resType==2 为文件夹 / md5）。

        page 从 0 开始，逐页 +1（见 list_folders 的说明）。
        """
        out: list[dict] = []
        page = 0
        while True:
            body = {
                "parentId": parent_id or "",
                "page": page,
                "pageSize": page_size,
                "orderBy": 1,
                "sortType": 0,
            }
            data = self._api_post("/userres/v1/file/get_file_list", body) or {}
            lst = data.get("list") or []
            if not lst:
                break
            for e in lst:
                out.append({
                    "file_id": (e.get("fileId") or "").strip(),
                    "name": (e.get("fileName") or "").strip(),
                    "size": int(e.get("fileSize") or 0),
                    "res_type": int(e.get("resType") or 0),
                    "md5": (e.get("md5") or "").strip(),
                    "parent_id": (e.get("parentId") or "").strip(),
                })
            total = data.get("total")
            if total and len(out) >= int(total):
                break
            if len(lst) < page_size:
                break
            page += 1
        return out


@dataclass
class SubmitResult:
    task_id: str = ""
    name: str = ""
    ok: bool = False
    message: str = ""
    skipped: bool = False
    reason: str = ""
    parent_id: str = ""


@dataclass
class PipelineContext:
    """提交一条磁力所需的上下文，供各模块传递信息。"""
    url: str = ""
    hash: str = ""
    title: str = ""
    year: int = 0
    kind: str = "other"          # movie / tv / other
    season: int = 0
    episode: int = 0
    resolution: str = ""
    target_dir: str = ""
    final_name: str = ""
    extra: dict = field(default_factory=dict)
