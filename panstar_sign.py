#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PanstarCloud 每日签到。

签到流程（与站点前端一致）：
  1. 取令牌：优先用缓存的 refreshToken 续期，失败再账号密码登录。
     站点 accessToken 只有 15 分钟有效期，但 refreshToken 可长期续期。
  2. 查今日状态：已签到就直接返回（不重复签）。
  3. 执行签到：
       a) 先试「免 ticket 直签」—— 服务端放行就完全不需要验证码；
       b) 被拒（718/741）才去拿 Turnstile token → preflight → 带 ticket 签到。

环境变量见 .env.example。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

from panstar_client import (
    CHECKIN_ACTION,
    CHECKIN_SITE_KEY,
    DOMAIN,
    NetworkError,
    PanstarClient,
    PanstarError,
    format_summary,
)
from turnstile_solver import TurnstileError, get_turnstile_token

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(APP_DIR, ".env")
STATE_DIR = os.path.join(APP_DIR, "data")
LOG_DIR = os.path.join(APP_DIR, "logs")


# --------------------------------------------------------------------------
# .env 读取
# --------------------------------------------------------------------------
def load_env_file(path: str = ENV_FILE) -> None:
    """把 .env 里的键值读进 os.environ（已存在的环境变量优先，不覆盖）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def cfg(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# 令牌缓存
# --------------------------------------------------------------------------
def state_path(account: str) -> str:
    safe = "".join(c for c in account if c.isalnum() or c in "@._-") or "default"
    return os.path.join(STATE_DIR, f"token-{safe}.json")


def load_state(account: str) -> Dict[str, Any]:
    path = state_path(account)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(account: str, data: Dict[str, Any]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(account), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"[state] 已保存 {state_path(account)}")


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------
def notify(title: str, content: str) -> None:
    """多渠道推送。任一渠道失败不影响其它渠道，也不影响签到结果。"""
    sent = False

    tg_token = cfg("TG_BOT_TOKEN")
    tg_user = cfg("TG_USER_ID")
    if tg_token and tg_user:
        try:
            import requests
            resp = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_user, "text": f"{title}\n\n{content}"},
                timeout=20,
            )
            print(f"[notify] Telegram: HTTP {resp.status_code}")
            sent = True
        except Exception as exc:
            print(f"[notify] Telegram 失败: {exc}")

    qywx = cfg("QYWX_KEY")
    if qywx:
        try:
            import requests
            resp = requests.post(
                f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={qywx}",
                json={"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}},
                timeout=20,
            )
            print(f"[notify] 企业微信: HTTP {resp.status_code}")
            sent = True
        except Exception as exc:
            print(f"[notify] 企业微信失败: {exc}")

    ding_token = cfg("DD_BOT_TOKEN")
    ding_secret = cfg("DD_BOT_SECRET")
    if ding_token:
        try:
            import requests
            url = f"https://oapi.dingtalk.com/robot/send?access_token={ding_token}"
            if ding_secret:
                import base64
                import hashlib
                import time as _t
                ts = str(round(_t.time() * 1000))
                secret_enc = base64.b64encode(ding_secret.encode()).decode()
                sign_str = f"{ts}\n{secret_enc}"
                sign = base64.b64encode(
                    hashlib.sha256(sign_str.encode()).digest()).decode()
                url += f"&timestamp={ts}&sign={sign}"
            resp = requests.post(url, json={"msgtype": "text",
                                            "text": {"content": f"{title}\n\n{content}"}},
                                 timeout=20)
            print(f"[notify] 钉钉: HTTP {resp.status_code}")
            sent = True
        except Exception as exc:
            print(f"[notify] 钉钉失败: {exc}")

    if not sent:
        print("[notify] 未配置任何通知渠道，只写日志")


# --------------------------------------------------------------------------
# 核心签到
# --------------------------------------------------------------------------
def agent_checkin(agent_url: str, agent_token: str, access_token: str = "",
                  refresh_token: str = "", username: str = "",
                  password: str = "", profile: str = "default",
                  timeout: int = 300) -> Dict[str, Any]:
    """让浏览器代理在浏览器里直接完成签到。

    这是最接近真人操作的路径：浏览器打开控制台 → 点 Daily Check-In
    → 点 Check In Now → 前端自己跑 Turnstile → preflight → 带 ticket 签到。
    """
    import requests

    headers = {"Content-Type": "application/json"}
    if agent_token:
        headers["Authorization"] = f"Bearer {agent_token}"

    body = {"profile": profile}
    if access_token:
        body["accessToken"] = access_token
        body["refreshToken"] = refresh_token
    else:
        body["username"] = username
        body["password"] = password

    resp = requests.post(
        f"{agent_url.rstrip('/')}/checkin",
        headers=headers,
        json=body,
        timeout=timeout,
    )
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"浏览器代理返回非 JSON: {resp.text[:200]}")
    if not data.get("ok"):
        raise RuntimeError(
            f"浏览器代理失败: {data.get('message') or data.get('error') or resp.text[:150]}"
        )
    return data.get("result") or {}


def sign_one(account: str, password: str, totp: str = "") -> Dict[str, Any]:
    """给单个账号签到，返回结果字典。"""
    print(f"\n{'=' * 60}")
    print(f"[account] {account}")
    print(f"{'=' * 60}")

    client = PanstarClient()

    # --- 1. 取令牌 ---
    state = load_state(account)
    cached_refresh = state.get("refreshToken", "")
    client.ensure_token(account, password, cached_refresh=cached_refresh, totp=totp)
    save_state(account, {
        "refreshToken": client.refresh_token,
        "accessToken": client.access_token,
        "uid": client.uid,
        "username": client.username,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })

    # --- 2. 查今日状态 ---
    status = client.checkin_status()
    if status.get("checkedIn"):
        summary = format_summary(status)
        print(f"[checkin] 今日已签到 → {summary}")
        return {"status": "already_checked", "summary": summary}

    print(f"[checkin] 今日未签到，当前累计 {status.get('summary', {}).get('totalPoints')} 分")

    # --- 3. 执行签到 ---
    result = None
    ticket = ""
    agent_url = cfg("LOGIN_AGENT_URL")
    agent_token = cfg("LOGIN_AGENT_TOKEN")
    profile_name = cfg("PS_PROFILE", "default")

    # 3a. 免 ticket 直签：服务端放行就完全不需要验证码
    print("[checkin] 先尝试免 ticket 直接签到")
    try:
        result = client.do_checkin("")
        print("[checkin] 免 ticket 签到成功，无需验证码")
    except PanstarError as exc:
        if exc.code not in (718, 719, 720, 741):
            raise
        print(f"[checkin] 服务端要求验证（{exc.code}），转浏览器流程")

    # 3b. 让浏览器代理直接在浏览器里完成整个签到（最接近真人操作）
    if result is None and agent_url:
        print("[checkin] 让浏览器代理直接完成签到")
        try:
            agent_result = agent_checkin(
                agent_url, agent_token,
                access_token=client.access_token,
                refresh_token=client.refresh_token,
                username=account, password=password,
                profile=profile_name,
            )
            flow = (agent_result.get("flow") or "").upper()
            if flow in ("SUCCESS", "CHECKED_IN", "ALREADY"):
                status = client.checkin_status()
                summary = format_summary(status)
                print(f"[checkin] 浏览器签到成功 → {summary}")
                return {"status": "success", "summary": summary}
            print(f"[checkin] 浏览器签到未成功（flow={flow}）")
        except Exception as exc:
            print(f"[checkin] 浏览器代理签到失败: {exc}")

    # 3c. 退一步：让浏览器只拿 Turnstile token，主脚本自己走 preflight → 签到
    if result is None:
        token = get_turnstile_token(
            site_key=cfg("PS_TURNSITE_SITE_KEY", CHECKIN_SITE_KEY),
            url=f"{DOMAIN}/console/dashboard",
            action=CHECKIN_ACTION,
            agent_url=agent_url,
            agent_token=agent_token,
            local_profile=cfg("PS_BROWSER_PROFILE", os.path.join(APP_DIR, "data", "browser")),
            headless=flag("PS_BROWSER_HEADLESS", False),
        )

        preflight = client.preflight(token)
        print(f"[checkin] preflight: {json.dumps(preflight, ensure_ascii=False)[:200]}")

        if preflight.get("alreadyChecked"):
            status = client.checkin_status()
            summary = format_summary(status)
            print(f"[checkin] preflight 提示今日已签到 → {summary}")
            return {"status": "already_checked", "summary": summary}

        if preflight.get("securityEnabled") is False:
            print("[checkin] preflight 说无需验证，免 ticket 签到")
            result = client.do_checkin("")
        else:
            ticket = preflight.get("ticket") or ""
            if not ticket:
                raise PanstarError(-1, "preflight 未返回 ticket")
            print("[checkin] 拿到 ticket，开始签到")
            result = client.do_checkin(ticket)

    summary = format_summary(result, status)
    print(f"[checkin] 签到成功 → {summary}")
    return {"status": "success", "summary": summary}


def parse_accounts() -> List[tuple]:
    """读取账号列表。

    两种写法：
      PS_USER_PASS=账号1&密码1&账号2&密码2   （推荐）
      PS_USER=xxx  +  PS_PASS=yyy          （单账号）
    """
    raw = cfg("PS_USER_PASS")
    if raw:
        items = [x for x in raw.split("&") if x.strip()]
        accounts = []
        for item in items:
            parts = item.split("&")[0] if "&" in item else item
            # 支持 user:pass 或 user=pass 两种写法
            if ":" in parts:
                user, pwd = parts.split(":", 1)
            elif "=" in parts:
                user, pwd = parts.split("=", 1)
            else:
                # 只有一个值：按 user&pass 交替解析
                continue
            accounts.append((user.strip(), pwd.strip()))
        if accounts:
            return accounts

    user, pwd = cfg("PS_USER"), cfg("PS_PASS")
    if user and pwd:
        return [(user, pwd)]
    return []


def main() -> int:
    load_env_file()
    os.makedirs(LOG_DIR, exist_ok=True)

    accounts = parse_accounts()
    if not accounts:
        print("❌ 没有配置账号。请设置 PS_USER_PASS 或 PS_USER + PS_PASS")
        print("   详见 .env.example")
        return 2

    print(f"[start] 站点 {DOMAIN}，共 {len(accounts)} 个账号")
    print(f"[start] {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = []
    failures = 0

    for account, password in accounts:
        try:
            res = sign_one(account, password, totp=cfg("PS_TOTP"))
            results.append((account, res["status"], res["summary"]))
        except (PanstarError, NetworkError, TurnstileError) as exc:
            failures += 1
            print(f"[error] {account}: {exc}")
            results.append((account, "failed", str(exc)))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[error] {account} 异常: {exc}")
            traceback.print_exc()
            results.append((account, "failed", f"{type(exc).__name__}: {exc}"))

    # --- 汇总 ---
    lines = [f"站点: {DOMAIN}", f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for account, status, summary in results:
        icon = {"success": "✅", "already_checked": "☑️"}.get(status, "❌")
        lines.append(f"{icon} {account}: {summary}")
    text = "\n".join(lines)

    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)

    notify("PanstarCloud 签到", text)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
