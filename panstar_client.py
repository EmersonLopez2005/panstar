# -*- coding: utf-8 -*-
"""
PanstarCloud (https://panstar.ai) API 客户端。

站点特点（全部实测过）：
  - 后端是 Next.js，不是 new-api/one-api。
  - 所有 /api/* 响应体都是 AES-GCM 加密的：
        响应头 x-api-encrypt: 1
        响应头 x-api-encrypt-iv: <base64 IV>
        响应体 {"ciphertext": "<base64>"}
    解密密钥硬编码在前端 bundle 里（模块 92591 的 v() 函数）。
  - 业务路径前缀是 /api/ecs/（不是 /api/），用错前缀会统一返回
    code=10 Token_Expired，看起来像"没登录"，极具误导性。
  - 刷新令牌走 /api/s0/auth/refresh-token（不带 ecs！）。
  - 签到需要 Cloudflare Turnstile token，服务端强制校验。
  - 浏览器来源校验：POST 必须带 Sec-Fetch-* 头，
    否则返回 403 code=741 TRUSTED_BROWSER_REQUEST_REQUIRED。
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

# 前端 bundle 里的硬编码解密密钥（base64 → 32 字节）
RESPONSE_DECRYPT_KEY = "26lAIOdVLW74vJFTawTAiA79kXTtdlibFh49gt+j5Zk="

DOMAIN = os.environ.get("PS_DOMAIN", "https://panstar.ai").rstrip("/")
API_PREFIX = "/api/ecs"
AUTH_PREFIX = "/api/s0"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# 站点签到用的 Turnstile site key 与 action。
# 前端 chunk 9122 渲染组件时写死 action="points_checkin"，invisible + execute 模式。
CHECKIN_SITE_KEY = os.environ.get("PS_TURNSITE_SITE_KEY", "0x4AAAAAAD4S6pSwBl0NuILe")
CHECKIN_ACTION = "points_checkin"


class PanstarError(Exception):
    """业务错误（携带站点返回的 code / messageKey）。"""

    def __init__(self, code: int, message: str, message_key: str = ""):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.message_key = message_key


class NetworkError(Exception):
    """网络/解密层错误（重试有意义）。"""


# --------------------------------------------------------------------------
# 响应解密
# --------------------------------------------------------------------------
def _decrypt_response(response: requests.Response) -> Any:
    """把加密响应解成 dict。响应不是加密格式时直接解析 JSON。"""
    text = response.text
    if response.headers.get("x-api-encrypt") != "1":
        return json.loads(text)

    iv_b64 = response.headers.get("x-api-encrypt-iv")
    if not iv_b64:
        raise NetworkError("响应带 x-api-encrypt 但缺少 IV 头")

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NetworkError(f"加密响应包不是合法 JSON: {text[:120]}") from exc

    ciphertext = envelope.get("ciphertext")
    if not ciphertext:
        raise NetworkError("加密响应缺少 ciphertext 字段")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover
        raise NetworkError(
            "缺少 cryptography 依赖，无法解密响应（pip install cryptography）"
        ) from exc

    key = base64.b64decode(RESPONSE_DECRYPT_KEY)
    iv = base64.b64decode(iv_b64)
    try:
        plain = AESGCM(key).decrypt(iv, base64.b64decode(ciphertext), None)
    except Exception as exc:
        raise NetworkError(f"响应解密失败: {exc}") from exc

    return json.loads(plain.decode("utf-8"))


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class PanstarClient:
    """封装登录 / 令牌续期 / 签到状态查询 / 签到执行。"""

    def __init__(self, session: Optional[requests.Session] = None,
                 timeout: int = 25):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.access_token = ""
        self.refresh_token = ""
        self.uid = 0
        self.username = ""
        self._use_fresh_token: Optional[str] = None

    # -- 底层请求 ---------------------------------------------------------
    def _headers(self, with_auth: bool = True, post: bool = False) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-Locale": "zh",
            "Accept-Language": "zh-CN",
            "User-Agent": BROWSER_UA,
            "Origin": DOMAIN,
            "Referer": f"{DOMAIN}/console/dashboard",
        }
        if post:
            # 服务端做「可信浏览器」校验（741）。缺 Sec-Fetch-Site 直接 403。
            headers["Sec-Fetch-Dest"] = "empty"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Site"] = "same-origin"
        if with_auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def request(self, method: str, path: str, body: Optional[dict] = None,
                with_auth: bool = True, post: bool = None) -> Any:
        """发一次请求并返回解密后的 data 字段。

        code != 0 时抛 PanstarError（带 code / messageKey）。
        """
        if post is None:
            post = method.upper() == "POST"
        url = f"{DOMAIN}{path}"
        try:
            resp = self.session.request(
                method.upper(),
                url,
                headers=self._headers(with_auth=with_auth, post=post),
                json=body if body is not None else None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NetworkError(f"请求失败 {method} {path}: {exc}") from exc

        try:
            payload = _decrypt_response(resp)
        except json.JSONDecodeError as exc:
            raise NetworkError(
                f"响应不是合法 JSON ({resp.status_code}): {resp.text[:150]}"
            ) from exc

        if isinstance(payload, dict):
            code = payload.get("code")
            if isinstance(code, int) and code != 0:
                raise PanstarError(
                    code,
                    payload.get("msg") or payload.get("message") or "未知错误",
                    payload.get("messageKey") or "",
                )
            return payload.get("data") if payload.get("data") is not None else payload

        return payload

    # -- 认证 -------------------------------------------------------------
    def login(self, username: str, password: str, totp: str = "") -> Dict[str, Any]:
        """账号密码登录。登录场景不需要 Turnstile（scenarios.userLogin=false）。"""
        body: Dict[str, Any] = {"username": username, "password": password}
        if totp:
            body["totpCode"] = totp
            body["rememberLogin"] = True
        data = self.request("POST", f"{API_PREFIX}/auth/login", body,
                            with_auth=False, post=False)
        self._apply_auth(data)
        return data

    def refresh(self, refresh_token: str) -> Dict[str, Any]:
        """用 refreshToken 换新令牌。站点每次刷新都会轮换 refreshToken。"""
        data = self.request("POST", f"{AUTH_PREFIX}/auth/refresh-token",
                            {"refreshToken": refresh_token}, with_auth=False, post=False)
        self._apply_auth(data)
        return data

    def _apply_auth(self, data: Dict[str, Any]) -> None:
        self.access_token = data.get("accessToken") or ""
        self.refresh_token = data.get("refreshToken") or ""
        self.uid = data.get("uid") or 0
        self.username = data.get("username") or ""
        if not self.access_token:
            raise PanstarError(-1, "登录/刷新响应里没有 accessToken")

    def ensure_token(self, username: str, password: str,
                     cached_refresh: str = "", totp: str = "") -> str:
        """拿一个可用令牌：优先 refreshToken 续期，失败再账号密码登录。

        返回实际使用的 refreshToken（会被站点轮换，必须存回缓存）。
        """
        if cached_refresh:
            try:
                self.refresh(cached_refresh)
                return self.refresh_token
            except (PanstarError, NetworkError) as exc:
                print(f"[auth] refresh 续期失败，改用账号密码登录: {exc}")
        self.login(username, password, totp)
        return self.refresh_token

    # -- 签到 -------------------------------------------------------------
    def checkin_status(self) -> Dict[str, Any]:
        """今日签到状态。"""
        return self.request("GET", f"{API_PREFIX}/points/checkin/today?limit=100")

    def turnstile_config(self) -> Dict[str, Any]:
        """Turnstile 配置（免鉴权）。"""
        return self.request("GET", f"{API_PREFIX}/auth/turnstile-config",
                            with_auth=False)

    def preflight(self, turnstile_token: str) -> Dict[str, Any]:
        """提交 Turnstile token，换取签到 ticket。

        服务端会在这里判定：alreadyChecked / securityEnabled / ticket。
        """
        return self.request("POST", f"{API_PREFIX}/points/checkin/preflight",
                            {"turnstileToken": turnstile_token})

    def do_checkin(self, ticket: str = "") -> Dict[str, Any]:
        """执行签到。ticket 为空表示走「免 ticket 直签」路径。"""
        body = {"ticket": ticket} if ticket else {}
        return self.request("POST", f"{API_PREFIX}/points/checkin?limit=100", body)

    def current_user(self) -> Dict[str, Any]:
        return self.request("GET", f"{API_PREFIX}/user/current")


# --------------------------------------------------------------------------
# 摘要格式化
# --------------------------------------------------------------------------
def format_summary(status: Optional[dict] = None,
                   result: Optional[dict] = None) -> str:
    """把签到结果整理成一句人话。"""
    source = result or status or {}
    record = source.get("record") or {}
    summary = source.get("summary") or {}
    lines = []

    fixed = record.get("fixedPoints")
    random_pts = record.get("randomPoints")
    total = record.get("totalPoints")
    if total is not None:
        detail = []
        if fixed is not None:
            detail.append(f"固定 {fixed}")
        if random_pts is not None:
            detail.append(f"随机 {random_pts}")
        suffix = f"（{' + '.join(detail)}）" if detail else ""
        lines.append(f"获得积分: {total}{suffix}")
    elif total is None and summary.get("totalPoints") is not None:
        lines.append(f"累计积分: {summary.get('totalPoints')}")

    level = summary.get("level")
    next_level = summary.get("nextLevel")
    next_need = summary.get("nextLevelMinPoints")
    if level is not None:
        text = f"等级: Lv.{level}"
        if next_level is not None:
            text += f"（下一级 Lv.{next_level} 需 {next_need} 分）"
        lines.append(text)

    rank = source.get("myRank")
    if rank is not None:
        lines.append(f"今日排名: 第 {rank} 名")

    if not lines:
        raw = json.dumps(source, ensure_ascii=False)
        lines.append(raw[:200] if raw != "{}" else "今日已签到")

    return "，".join(lines)
