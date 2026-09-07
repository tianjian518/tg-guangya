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

import re
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

# 目录列表排序参数（对齐 LitePan drivers/Guangya transport.go 的默认值，
# 也与 OpenList 光鸭驱动配置里的 order_by=3 / sort_type=1 一致）
LIST_ORDER_BY = 3
LIST_SORT_TYPE = 1

# 业务接口最小请求间隔（秒）。光鸭对高频调用会限流（HTTP 429 / 业务码 354），
# LitePan 使用 defaultOperationDelayMS = 300 做同样的保护。
OP_INTERVAL = 0.3

# 光鸭的限流业务错误码（LitePan mapAPIError）
CODE_RATE_LIMIT = 354
# 异步任务（删除/移动/复制）状态：2=完成，-1/3/5=失败
TASK_DONE = 2
TASK_FAILED_STATUSES = (-1, 3, 5)

# ---------- 分享转存专用业务码（2026-09 光鸭 Web 前端 bundle 逆向 + 官方埋点对齐）----------
# get_share_summary 的 allowCode（非 0 但不算异常，前端按状态分流）：
#   200/201 = 分享不存在或已失效（前端显示 invalid）
#   202     = 分享已过期（前端显示 expired）
SHARE_CODE_INVALID = 209      # get_share_access_token：提取码错误（前端转入"verifying"）
SHARE_STATUS_INVALID = (200, 201)
SHARE_STATUS_EXPIRED = 202


class GuangyaError(Exception):
    """光鸭接口错误。"""


class GuangyaBizError(GuangyaError):
    """光鸭业务错误（信封 code != 0）。

    与普通 GuangyaError 的区别：保留信封里的业务码 code 与原始 msg，
    供分享链路区分「提取码错误(209)/分享过期(202)/分享失效(200,201)」等
    需要差异化处理的状态。所有 except GuangyaError 的旧代码不受影响。
    """

    def __init__(self, code: int, msg: str = "", envelope: dict | None = None) -> None:
        super().__init__(msg or f"光鸭业务错误 {code}")
        self.code = code
        self.msg = msg
        self.envelope = envelope or {}


class AuthExpired(GuangyaError):
    """令牌失效，需要重新扫码登录。"""


def parse_share_url(url: str) -> dict | None:
    """识别光鸭分享链接，返回 {share_id, code, share_code}；非分享链接返回 None。

    真实链接格式（2026-09 官方/社区样本实测）：
      https://www.guangyapan.com/s/1894410604530630727_aeXsY5wocgzRgFTv
      https://www.guangyapan.com/s/189...?code=jiif        （提取码在 ?code=）
      https://www.guangyapan.com/share/<shareId>           （前端 SPA 路由，亦兼容）
      https://app.guangyapan.com/share/<shareId>?shareCode=<口令>
    shareId = 匹配路径的最后一段（对齐前端 IL hook 的解析逻辑）。
    提取码与口令可能同时存在，分别对应 get_share_access_token 的 code
    与 restore_share 的 shareCode。
    """
    m = re.search(
        r"https?://(?:[a-z0-9-]+\.)*guangyapan\.com/(?:share|s)/([A-Za-z0-9_-]+)",
        (url or "").strip(), re.I,
    )
    if not m:
        return None
    share_id = m.group(1)

    # query 里的提取码/口令：截至 & 或空白/中文括号（分享链接常被塞在长文本里）
    def _q(key: str) -> str:
        qm = re.search(rf"[?&]{key}=([^&\s\"'<>（）【】]+)", url, re.I)
        return qm.group(1).strip() if qm else ""

    return {
        "share_id": share_id,
        "code": _q("code"),
        "share_code": _q("shareCode"),
    }


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

    # 任务状态码（与模块级常量保持一致，供 GuangyaClient.STATUS_* 访问）
    STATUS_PENDING = STATUS_PENDING
    STATUS_RUNNING = STATUS_RUNNING
    STATUS_SUCCESS = STATUS_SUCCESS
    STATUS_FAILED = STATUS_FAILED
    STATUS_RETRYING = STATUS_RETRYING
    STATUS_FAILED_ALT = STATUS_FAILED_ALT

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
        self._last_api_at = 0.0  # 上次业务请求时间，用于节流
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

    # ---------- 短信验证码登录（手机号 + 短信码）----------

    def sms_send_code(self, phone: str, captcha_token: str = "") -> str:
        """向手机号发送短信验证码，返回 verification_id。

        端点：POST /v1/auth/verification（对齐 Web 端 JS 逆向，
        body 顶层 {phone_number: "+86 xxx", target: "ANY"}）。

        ⚠️ 2026-09 实测：该接口被服务端强制要求图形验证码——缺少时返回
        {"error": "captcha_required"}。合法的 captcha token 由浏览器过滑块后
        写入 localStorage，SDK 通过 x-captcha-token 请求头携带。因此**纯脚本
        环境走不通短信登录**，请用扫码登录（python login.py）。

        captcha_token: 浏览器过完图形验证码后取得的 token（可选，服务端可用时传）。
        """
        headers = {"x-captcha-token": captcha_token} if captcha_token else None
        body = {"phone_number": self._cn_phone(phone), "target": "ANY"}
        data = self._account_post("/v1/auth/verification", body, extra_headers=headers) or {}
        vid = (data.get("verificationId") or data.get("verification_id") or "").strip()
        if not vid:
            raise GuangyaError(f"光鸭发短信验证码失败，返回: {data}")
        return vid

    def sms_login(self, phone: str, sms_code: str, verification_id: str,
                  captcha_token: str = "") -> tuple[str, str]:
        """用短信验证码登录，返回 (access_token, refresh_token)。

        完整三步（对齐 Web 端 JS 逆向）：
          ① sms_send_code → verification_id（需图形验证码，见上）
          ② POST /v1/auth/verification/verify {verification_id, verification_code}
             → verification_token
          ③ POST /v1/auth/signin {verification_code, verification_token,
             username: "+86 xxx"}
        同样受图形验证码限制，纯脚本环境请用扫码登录（python login.py）。
        """
        headers = {"x-captcha-token": captcha_token} if captcha_token else None
        vbody = {"verificationId": verification_id, "verificationCode": str(sms_code)}
        # verify 端点的字段名未在真实环境验证过（发码步已被 captcha 挡住），
        # 两种命名都试一下，成功为准。
        vtoken = ""
        last_err: Exception | None = None
        for payload in (
            vbody,
            {"verification_id": verification_id, "verification_code": str(sms_code)},
        ):
            try:
                data = self._account_post("/v1/auth/verification/verify", payload,
                                          extra_headers=headers) or {}
            except GuangyaError as exc:
                last_err = exc
                continue
            vtoken = (data.get("verificationToken") or data.get("verification_token")
                      or data.get("token") or "").strip()
            if vtoken:
                break
        if not vtoken:
            raise GuangyaError(f"光鸭短信码校验失败: {last_err}")

        body = {
            "username": self._cn_phone(phone),
            "verification_code": str(sms_code),
            "verification_token": vtoken,
        }
        data = self._account_post("/v1/auth/signin", body, extra_headers=headers) or {}
        access = self._first_token(data, "access_token", "accessToken", "token")
        refresh = self._first_token(data, "refresh_token", "refreshToken")
        if not access:
            raise GuangyaError(f"光鸭短信登录失败，返回: {data}")
        self._access = access
        if refresh:
            self._refresh = refresh
        self._expire_at = time.time() + 7200 - 900
        return access, refresh

    @staticmethod
    def _first_token(data: dict, *keys: str) -> str:
        """兼容 token 在顶层或 data 层两种返回结构。"""
        if not isinstance(data, dict):
            return ""
        for k in keys:
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        sub = data.get("data")
        if isinstance(sub, dict):
            for k in keys:
                v = sub.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

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
        if resp.status_code == 429:
            raise GuangyaError("光鸭接口限流（HTTP 429），请稍后重试")
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
        if isinstance(envelope.get("code"), int) and envelope["code"] == CODE_RATE_LIMIT:
            raise GuangyaError(f"光鸭接口限流（{CODE_RATE_LIMIT}）：{envelope.get('msg') or '请稍后重试'}")
        # 业务接口信封：{code, msg, data:{...}}（部分接口也带 success 字段）
        code = envelope.get("code")
        if isinstance(code, int) and code != 0:
            raise GuangyaBizError(
                code, envelope.get("msg") or envelope.get("message") or f"光鸭业务错误 {code}",
                envelope,
            )
        if envelope.get("success") is False:
            raise GuangyaBizError(
                -1, envelope.get("message") or f"光鸭错误 {envelope.get('code')}", envelope,
            )
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

    @staticmethod
    def _cn_phone(phone: str) -> str:
        """归一成光鸭要求的 "+86 xxxxxxxxxx"（带空格）格式。"""
        s = str(phone).strip()
        if s.startswith("+86"):
            s = s[3:].strip()
        return "+86 " + s

    def _account_post(self, path: str, body: dict, auth: bool = False,
                      extra_headers: dict | None = None) -> Any:
        # raw=True：账户接口返回顶层 JSON，不做 data 拆包
        headers = self.build_account_headers()
        if extra_headers:
            headers = {**headers, **extra_headers}
        return self._post(ACCOUNT_BASE, path, body, auth=False,
                          headers=headers, raw=True)

    def _throttle(self) -> None:
        """业务接口节流：避免高频调用触发光鸭限流（HTTP 429 / 业务码 354）。"""
        gap = time.time() - self._last_api_at
        if 0 < gap < OP_INTERVAL:
            time.sleep(OP_INTERVAL - gap)
        self._last_api_at = time.time()

    def _api_post(self, path: str, body: dict) -> Any:
        self.ensure_token()
        self._throttle()
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

    def create_offline_task(self, url: str, parent_id: str = "",
                            cn_name: str = "", resolved: dict | None = None) -> tuple[str, str]:
        """提交离线下载，返回 (task_id, 解析出的英文原名)。

        cn_name 为可选的中文文件名。⚠️ 2026-09 实测：光鸭服务端会忽略该字段
        （LitePan 的 create_task 同样不传名字，任务名由服务端按资源内容生成），
        所以「下载时直接指定中文名」走不通，中文名靠任务完成后 rename_file 补做
        （见 main.py 监控线程）。保留传参无害，留作未来服务端放开时的兼容。
        resolved 可传入已解析结果以复用，避免重复调用 resolve 接口。
        """
        if resolved is None:
            resolved = self.resolve(url)
        body = {
            "url": url,
            "parentId": parent_id or "",
            "resType": resolved.get("res_type", 0),
        }
        if cn_name:
            body["fileName"] = cn_name
        data = self._api_post("/cloudcollection/v1/create_task", body) or {}
        task_id = (data.get("taskId") or "").strip()
        if not task_id:
            raise GuangyaError("光鸭未返回 taskId")
        return task_id, resolved.get("name", "")

    def rename_file(self, file_id: str, new_name: str) -> None:
        """重命名网盘文件/目录。

        接口：POST /userres/v1/file/rename
        body 字段名以 LitePan drivers/Guangya/ops.go 的 RenameFile 为准：
          {"fileId": ..., "newName": ...}

        ⚠️ 字段名是 `newName` 而非 `fileName`——2026-09 已用真实账号实测：
        传 fileName 服务端报「文件名不能为空」；传 newName 改名真实生效
        （改名后重新列举目录可查到新名，旧名消失）。

        失败会抛 GuangyaError，由调用方决定是否降级为保留原名。
        """
        if not file_id or not new_name:
            return
        self._api_post("/userres/v1/file/rename", {"fileId": file_id, "newName": new_name})

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
            "orderBy": LIST_ORDER_BY,
            "sortType": LIST_SORT_TYPE,
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

    def wait_task(self, task_id: str, timeout: int = 30) -> bool:
        """等待光鸭异步任务（删除/移动/复制）完成。

        这些接口会返回 taskId，需要轮询 /userres/v1/get_task_status，
        status == 2 表示完成，-1/3/5 表示失败（对齐 LitePan ops.go waitTaskDone）。
        返回 True 表示任务完成；超时返回 False。
        """
        task_id = (task_id or "").strip()
        if not task_id:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self._throttle()
                self.ensure_token()
                data = self._post(
                    API_BASE, "/userres/v1/get_task_status",
                    {"taskId": task_id}, auth=True,
                ) or {}
                status = int(data.get("status") or 0)
                if status == 2:
                    return True
                if status in (-1, 3, 5):
                    return False
            except GuangyaError as exc:
                log.warning("查询任务状态失败 %s: %s", task_id, exc)
                return False
            time.sleep(1)
        return False

    def wait_offline_task(self, task_id: str, timeout: int = 120, poll_interval: int = 10) -> tuple[int, str]:
        """等待离线下载任务完成，返回 (最终状态码, 消息)。

        离线任务状态码（来自 LitePan mapOfflineTaskUpdate）：
          0 = 等待处理, 1 = 离线下载中, 2 = 已完成,
          3 = 失败, 4 = 重试中, 5 = 失败

        超时时返回 (当前状态, "等待超时")；失败时返回对应失败码和错误信息。
        """
        task_id = (task_id or "").strip()
        if not task_id:
            return STATUS_FAILED, "缺少 taskId"
        deadline = time.time() + timeout
        last_msg = ""
        while time.time() < deadline:
            try:
                tasks = self.list_tasks()
                for t in tasks:
                    if t.task_id != task_id:
                        continue
                    if t.status == STATUS_SUCCESS:
                        return STATUS_SUCCESS, "已完成"
                    if t.status in (STATUS_FAILED, STATUS_FAILED_ALT):
                        return t.status, t.message or "离线下载失败"
                    last_msg = t.message or STATUS_TEXT.get(t.status, "")
                # 任务不在列表中（可能被清理），视为进行中
                time.sleep(poll_interval)
            except GuangyaError as exc:
                log.warning("轮询离线任务状态失败 %s: %s", task_id, exc)
                time.sleep(poll_interval)
        return -1, f"等待超时（{timeout}s），当前状态: {last_msg or '未知'}"

    def delete_file(self, parent_id: str, file_id: str) -> None:
        """删除网盘里的文件/目录（用于洗版时替换旧版本）。

        接口（对齐 LitePan transport.go 的 pathDeleteFile）：
          POST /userres/v1/file/delete_file   body {"fileIds": [...]}

        注意两点（都经过实测确认）：
        1. 路径是 **delete_file**。早期版本用的 /userres/v1/file/delete 实测返回
           HTTP 404（接口根本不存在），会导致洗版时「旧的删不掉、新的又存一份」。
        2. 删除是**异步任务**，返回 taskId，必须轮询等它完成，否则紧接着提交的
           转存可能撞上还没删掉的旧文件。

        另：光鸭默认把文件移到回收站（LitePan deleteMode 默认 trash），并非立即抹除。
        """
        if not file_id:
            raise GuangyaError("删除文件缺少 fileId")
        data = self._api_post(
            "/userres/v1/file/delete_file",
            {"fileIds": [file_id]},
        ) or {}
        task_id = (data.get("taskId") or "").strip()
        if task_id:
            self.wait_task(task_id)
        log.info("已删除光鸭文件: %s", file_id)

    def move_file(self, file_id: str, target_parent_id: str) -> None:
        """把文件/文件夹移动到目标目录（用于剧集单集收进剧名文件夹）。

        接口（对齐 LitePan transport.go pathMoveFile / ops.go moveViaTask）：
          POST /userres/v1/file/move_file   body {"fileIds": [...], "parentId": 目标}
        异步任务（实测返回 taskId，状态 2=完成），等待完成后再返回。
        2026-09 已用真实账号实测：文件夹移动往返成功、零残留。
        """
        if not file_id or not target_parent_id:
            return
        data = self._api_post("/userres/v1/file/move_file",
                              {"fileIds": [file_id], "parentId": target_parent_id}) or {}
        task_id = (data.get("taskId") or "").strip()
        if task_id:
            self.wait_task(task_id)
        log.info("已移动光鸭文件 %s → 目录 %s", file_id, target_parent_id)

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
                "orderBy": LIST_ORDER_BY,
                "sortType": LIST_SORT_TYPE,
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

    # ---------- 分享链接转存（2026-09 光鸭 Web 前端 bundle 逆向 + 官方埋点对齐）----------
    #
    # 全链路（与前端 share 页面行为一致）：
    #   ① get_share_summary        {shareId}                        → needCode/shareStatus
    #   ② get_share_access_token   {shareId, code}                  → accessToken（209=提取码错）
    #   ③ get_share_page_files_list {accessToken, parentId:"", pageSize, cursor} → 文件列表
    #   ④ restore_share            {accessToken, fileIds, parentId, shareCode?} → taskId
    #   ⑤ get_task_status 轮询     {taskId}                         → status==2 完成
    #
    # 官方埋点（pan_share_restore_task_create / pan_share_restore_task_completed）
    # 确认 restore_share 就是「转存到自己网盘」的接口；埋点载荷字段
    # share_id / restore_task_id / kouling（口令）与上述链路一一对应。

    @staticmethod
    def parse_share_url(url: str) -> dict | None:
        """识别光鸭分享链接；实现见模块级 parse_share_url（测试/调用方均可直接用）。"""
        return parse_share_url(url)

    def get_share_summary(self, share_id: str) -> dict:
        """分享摘要（needCode / shareStatus / shareName 等）。

        分享失效(200,201)/过期(202)会抛 GuangyaBizError，code 属性可判断。
        """
        return self._api_post("/userres/v1/get_share_summary", {"shareId": share_id}) or {}

    def get_share_access_token(self, share_id: str, code: str = "") -> str:
        """用分享 id + 提取码换取分享访问令牌（后续文件列表/转存都用它鉴权）。"""
        body: dict[str, Any] = {"shareId": share_id}
        if code:
            body["code"] = code
        data = self._api_post("/userres/v1/get_share_access_token", body) or {}
        token = (data.get("accessToken") or "").strip()
        if not token:
            raise GuangyaError("光鸭未返回分享 accessToken")
        return token

    def list_share_files(self, access_token: str, parent_id: str = "",
                         page_size: int = 100, cursor: str = "") -> dict:
        """列出分享内的文件/文件夹（cursor 分页）。

        body 结构来自前端文件浏览 hook（bx/by）：{...params, parentId} +
        cursor 模式附加 {cursor}；根目录 parentId=""。响应 {list, hasMore, cursor}。
        """
        body: dict[str, Any] = {
            "accessToken": access_token,
            "parentId": parent_id or "",
            "pageSize": page_size,
            "orderBy": 0,
            "sortType": 0,
        }
        if cursor:
            body["cursor"] = cursor
        return self._api_post("/userres/v1/get_share_page_files_list", body) or {}

    def list_share_all_files(self, access_token: str, parent_id: str = "") -> list[dict]:
        """拉全分享目录（自动 cursor 翻页），返回与 list_dir 同构的条目列表。

        真实响应（2026-09 实测）：{total, list, cursor}——
          - cursor 是【数字】且可能为 0（falsy），必须显式 None 检查；
          - 没有 hasMore 字段 → 翻页条件用 total（与 list_dir 同口径）；
        双层防护（服务端分页异常时保命）：
          1. 已用过的请求 cursor 不得再次发起（原地打转 → 立即停）
          2. 条目按 fileId 去重（服务端重复返回同一页时不产生重复条目）
        """
        out: list[dict] = []
        seen_ids: set[str] = set()
        cursor: Any = ""          # 请求参数原样传递（首页 ""，后续回传服务端给的值）
        used_keys: set[str] = {""}
        total: int | None = None
        while True:
            data = self.list_share_files(access_token, parent_id, cursor=cursor)
            for e in data.get("list") or []:
                fid = (e.get("fileId") or "").strip()
                if fid and fid in seen_ids:
                    continue
                if fid:
                    seen_ids.add(fid)
                out.append({
                    "file_id": fid,
                    "name": (e.get("fileName") or "").strip(),
                    "size": int(e.get("fileSize") or 0),
                    "res_type": int(e.get("resType") or 0),
                    "parent_id": (e.get("parentId") or "").strip(),
                })
            if isinstance(data.get("total"), int):
                total = data["total"]
            # 翻页判定：total 已抓满 / 无 cursor / cursor 空或 0 / 原地打转 → 停
            if total is not None and len(out) >= total:
                break
            if "cursor" not in data or data.get("cursor") is None:
                break
            raw = data["cursor"]
            key = str(raw).strip()
            if not key or key == "0" or key in used_keys:
                break
            used_keys.add(key)
            cursor = raw  # 服务端给 int 就回传 int（实测 cursor 为数字）
        return out

    def restore_share(self, access_token: str, file_ids: list[str], parent_id: str,
                      share_code: str = "") -> str:
        """把分享内容转存到自己网盘的 parent_id 目录，返回异步任务 taskId。

        fileIds 是分享内条目（文件或整个文件夹，文件夹会整体递归转存）。
        shareCode 为口令分享时的口令值（普通链接分享无需传）。
        返回的 taskId 用 wait_task() 轮询（status==2 完成，与 move/delete 同一套）。
        """
        if not file_ids:
            raise GuangyaError("转存分享缺少 fileIds")
        body: dict[str, Any] = {
            "accessToken": access_token,
            "fileIds": list(file_ids),
            "parentId": parent_id or "",
        }
        if share_code:
            body["shareCode"] = share_code
        data = self._api_post("/userres/v1/restore_share", body) or {}
        return (data.get("taskId") or "").strip()

    def save_share(self, url: str, parent_id: str, timeout: int = 120) -> dict:
        """识别分享链接并整体转存到 parent_id，返回执行摘要。

        返回 {ok, share_id, task_id, files, entries, message}：
          files   = 本次转存的分享根条目数
          entries = 转存条目的 [{file_id, name, res_type}]（供调用方做中文命名）
        提取码从链接 query 自动携带；没有提取码且需要时服务端会报
        GuangyaBizError(209)（"需要提取码"），由调用方决定提示话术。
        """
        parsed = self.parse_share_url(url)
        if not parsed:
            return {"ok": False, "share_id": "", "task_id": "", "files": 0,
                    "entries": [], "message": "不是光鸭分享链接"}
        share_id = parsed["share_id"]
        try:
            self.get_share_summary(share_id)
        except GuangyaBizError as exc:
            if exc.code in SHARE_STATUS_INVALID:
                return {"ok": False, "share_id": share_id, "task_id": "", "files": 0,
                        "entries": [], "message": "分享不存在或已失效"}
            if exc.code == SHARE_STATUS_EXPIRED:
                return {"ok": False, "share_id": share_id, "task_id": "", "files": 0,
                        "entries": [], "message": "分享已过期"}
            raise
        access = self.get_share_access_token(share_id, parsed["code"])
        entries = self.list_share_all_files(access)
        if not entries:
            return {"ok": False, "share_id": share_id, "task_id": "", "files": 0,
                    "entries": [], "message": "分享内容为空"}
        file_ids = [e["file_id"] for e in entries if e["file_id"]]
        task_id = self.restore_share(access, file_ids, parent_id,
                                     share_code=parsed["share_code"])
        ok = self.wait_task(task_id, timeout=timeout) if task_id else False
        return {
            "ok": ok,
            "share_id": share_id,
            "task_id": task_id,
            "files": len(file_ids),
            "entries": entries,
            "message": "" if ok else ("转存任务已提交，等待结果" if task_id else "光鸭未返回转存任务号"),
        }


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
