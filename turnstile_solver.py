# -*- coding: utf-8 -*-
"""
Turnstile token 获取器。

**本项目不需要打码服务。** 站点签到要的 Cloudflare Turnstile token，
由自带的浏览器（camoufox 反检测 + 有头 + 持久化 profile）自己跑出来。

为什么不用打码服务：
  打码服务产出的 token 在这个站点会因「来源 IP / UA 与请求方不匹配」被服务端判无效；
  而浏览器方案产出的 token 天然自洽（同一个浏览器、同一个出口 IP、同一个 UA）。

按优先级依次尝试，任何一条路拿到 token 就返回：

  1. 浏览器代理（LOGIN_AGENT_URL）—— 推荐
     常驻的浏览器服务去跑。它跑在有头浏览器 + 持久化 profile + 干净出口 IP 里，
     是过 Cloudflare 最稳的方式。见 agent/ 目录。
  2. 本地浏览器 —— 在本机起一个同样的浏览器（需装 camoufox）。

两条路都会用与站点前端完全一致的 Turnstile 参数
（action=points_checkin / execution=execute / invisible / retry=never），
否则服务端可能判 token 无效。
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests


class TurnstileError(Exception):
    """所有路径都拿不到 token 时抛出。"""


# --------------------------------------------------------------------------
# 路径 1：浏览器代理
# --------------------------------------------------------------------------
def solve_via_agent(agent_url: str, agent_token: str, site_key: str,
                    url: str, action: str = "",
                    timeout: int = 240) -> str:
    """让浏览器代理服务跑一次 Turnstile，返回 token。

    代理接口：
        POST {agent_url}/turnstile
        {"siteKey": ..., "url": ..., "action": ...}
        → {"ok": true, "token": "..."}
    """
    headers = {"Content-Type": "application/json"}
    if agent_token:
        headers["Authorization"] = f"Bearer {agent_token}"

    resp = requests.post(
        f"{agent_url.rstrip('/')}/turnstile",
        headers=headers,
        json={"siteKey": site_key, "url": url, "action": action},
        timeout=timeout,
    )
    try:
        data = resp.json()
    except Exception:
        raise TurnstileError(f"浏览器代理返回非 JSON: {resp.text[:200]}")
    if not data.get("ok"):
        raise TurnstileError(
            f"浏览器代理失败: {data.get('message') or data.get('error') or resp.text[:150]}"
        )
    token = data.get("token") or ""
    if not token:
        raise TurnstileError("浏览器代理未返回 token")
    return token


# --------------------------------------------------------------------------
# 路径 2：本地浏览器
# --------------------------------------------------------------------------
def solve_via_local_browser(site_key: str, url: str, action: str = "",
                             profile_dir: str = "",
                             headless: bool = False,
                             timeout: int = 180) -> str:
    """本机起反检测浏览器，在真实页面里跑一次 Turnstile。

    要点（都来自实测）：
      - 必须有头（headless=False）+ Xvfb，headless 一定被 Cloudflare 判 bot；
      - 必须用持久化 profile，指纹稳定才能过；
      - 优先 camoufox（Firefox 定制版，指纹伪装强），
        其次 patchright / playwright。
    """
    # 先确认引擎真的可用（浏览器本体已下载），避免运行到一半才去下 1.3GB。
    try:
        from browser_engine import engine_status, missing_browser_hint
    except ImportError:
        engine_status = None

    if engine_status:
        status = engine_status()
        usable = [n for n, i in status.items() if i["package"] and i["binary"]]
        if not usable:
            raise TurnstileError(
                "本地浏览器不可用（浏览器本体未下载）:\n"
                + missing_browser_hint()
            )

    try:
        from browser_engine import browser_page
    except ImportError:
        browser_page = None

    if browser_page is not None:
        with browser_page(engine="auto", headless=headless,
                          user_data_dir=profile_dir or None) as page:
            return _drive_turnstile(page, site_key, url, action, timeout)

    # 没装 browser_engine 就直接用 camoufox
    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:
        raise TurnstileError(
            "本地浏览器不可用：未安装 camoufox（pip install camoufox[geoip] "
            "&& python -m camoufox fetch）"
        ) from exc

    with Camoufox(headless=headless, humanize=True) as browser:
        ctx = browser.new_context()
        page = ctx.new_page()
        return _drive_turnstile(page, site_key, url, action, timeout)


# 在页面里渲染 Turnstile widget 并等 token。
# 站点用的是 invisible + execute 模式：
#   - invisible: widget 不显示，token 通过 callback 返回（response-field=false）；
#   - execute: 需要显式调用 execute() 才会触发挑战。
# 参数与站点前端组件（chunk 9122）保持一致。
_TS_SCRIPT = """
async (args) => {
  const { siteKey, action } = args;
  return await new Promise((resolve) => {
    const out = { token: null, err: null };
    const done = () => resolve(out);
    const s = document.createElement('script');
    s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    s.onload = () => {
      try {
        const div = document.createElement('div');
        div.id = 'ts-probe';
        document.body.appendChild(div);
        const opts = {
          sitekey: siteKey,
          action: action || undefined,
          execution: 'execute',
          invisible: true,
          appearance: 'execute',
          retry: 'never',
          'refresh-expired': 'never',
          'response-field': false,
          callback: (t) => { out.token = t; done(); },
          'error-callback': (e) => { out.err = 'error:' + e; done(); },
          'unsupported-callback': () => { out.err = 'unsupported'; done(); },
          'timeout-callback': () => { out.err = 'timeout'; done(); },
        };
        const w = window.turnstile.render('#ts-probe', opts);
        // execute 模式不会自动跑，必须手动 execute()
        try { w.execute(); } catch (e) { out.err = 'exec:' + e.message; done(); }
      } catch (e) { out.err = 'catch:' + e.message; done(); }
    };
    s.onerror = () => { out.err = 'script-load-failed'; done(); };
    document.head.appendChild(s);
    setTimeout(() => { out.err = out.err || 'timeout'; done(); }, 60000);
  });
}
"""


def _drive_turnstile(page, site_key: str, url: str, action: str,
                     timeout: int) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    time.sleep(5)
    result = page.evaluate(_TS_SCRIPT, {"siteKey": site_key, "action": action})
    token = (result or {}).get("token")
    if token:
        return token
    raise TurnstileError(
        f"本地浏览器未取得 token: {(result or {}).get('err') or '未知原因'}"
    )


def check_agent_health(agent_url: str, agent_token: str = "") -> dict:
    """检查浏览器代理是否在线。

    返回 {"ok", "engine", "busy", "auth_ok", "message"}。

    注意 **auth_ok** 这个字段：/health 接口本身不鉴权，所以
    「agent 在线」不代表「token 是对的」。必须额外用 token 调一次
    需要鉴权的接口，才能确认两边 token 一致。
    否则会出现这种情况：agent 在线 → 真正签到时 401 →
    被误报成「内存不足，浏览器方案不可行」。
    """
    try:
        headers = {}
        if agent_token:
            headers["Authorization"] = f"Bearer {agent_token}"
        resp = requests.get(f"{agent_url.rstrip('/')}/health", headers=headers, timeout=8)
        if resp.status_code != 200:
            return {"ok": False, "engine": "", "busy": False, "auth_ok": False,
                    "message": f"HTTP {resp.status_code}"}
        data = resp.json()
        result = {
            "ok": bool(data.get("ok")),
            "engine": data.get("engine", ""),
            "busy": bool(data.get("busy", False)),
            "auth_ok": True,
            "message": data.get("message", ""),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "engine": "", "busy": False, "auth_ok": False,
                "message": str(exc)}

    # 用 token 探一次 /auth-check（不启动浏览器，只校验 token）
    if agent_token:
        try:
            probe = requests.get(
                f"{agent_url.rstrip('/')}/auth-check",
                headers={"Authorization": f"Bearer {agent_token}"},
                timeout=8,
            )
            if probe.status_code == 401:
                result["auth_ok"] = False
                result["message"] = "token 不正确（401）"
        except Exception:
            # 探测失败不影响判断，auth_ok 保持 True
            pass

    return result


def _available_memory_mb() -> int:
    """系统可用内存（MB）。读不到返回一个大数。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 99999


def _can_start_local_browser() -> bool:
    """本机还有没有余量再起一个浏览器。

    小内存机器（1 核 / 1GB）上，agent 的浏览器刚失败退出、
    内存还没释放干净，这时再起一个本地浏览器会直接 swap 抖动 ——机器卡死。
    """
    avail = _available_memory_mb()
    if avail < 600:
        print(f"[turnstile] ⚠️ 可用内存仅 {avail} MB，不再启动本地浏览器"
              "（会引发 swap 抖动把机器拖死）")
        return False
    return True


# --------------------------------------------------------------------------
# 统一入口：按优先级依次尝试
# --------------------------------------------------------------------------
def get_turnstile_token(site_key: str, url: str, action: str = "",
                        agent_url: str = "", agent_token: str = "",
                        local_profile: str = "", headless: bool = False) -> str:
    """依次尝试各条路径，返回第一个成功的 token。

    每条路径的失败都会被记录下来，最终全失败时把原因一起抛出，
    便于判断到底卡在哪一步。
    """
    attempts = []

    if agent_url:
        # 先探一下 agent 是否在线，失败要给出可操作的提示，
        # 而不是一句"连接失败"让人摸不着头脑。
        health = check_agent_health(agent_url, agent_token)
        if not health["ok"]:
            msg = f"浏览器代理不可用 ({health['message']})"
            attempts.append(msg)
            print(f"[turnstile] {msg}")
            print("[turnstile]   启动它：systemctl start panstar-agent"
                  " 或 cd agent && docker compose up -d")
        else:
            try:
                print(f"[turnstile] 尝试路径1：浏览器代理（engine={health['engine']}）")
                token = solve_via_agent(agent_url, agent_token, site_key, url, action)
                print(f"[turnstile] 浏览器代理成功 (token {len(token)} 字符)")
                return token
            except Exception as exc:
                attempts.append(f"浏览器代理: {exc}")
                print(f"[turnstile] 浏览器代理失败: {exc}")

    # 路径 2：本地浏览器
    # agent 刚失败过就不要再叠加一个浏览器实例 —— 小内存机器会卡死。
    if agent_url and not _can_start_local_browser():
        attempts.append("本地浏览器: 可用内存不足，跳过（避免与 agent 抢内存导致机器卡死）")
    else:
        try:
            print("[turnstile] 尝试路径2：本地浏览器")
            token = solve_via_local_browser(
                site_key, url, action,
                profile_dir=local_profile,
                headless=headless,
            )
            print(f"[turnstile] 本地浏览器成功 (token {len(token)} 字符)")
            return token
        except Exception as exc:
            attempts.append(f"本地浏览器: {exc}")
            print(f"[turnstile] 本地浏览器失败: {exc}")

    raise TurnstileError(
        "所有 Turnstile 通道都失败:\n  " + "\n  ".join(attempts or ["没有配置任何通道"])
    )
