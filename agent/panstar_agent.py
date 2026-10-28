#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PanstarCloud 浏览器代理服务（常驻）。

为什么需要它：
  panstar 的签到要过 Cloudflare Turnstile，而 Turnstile 在
  **自动化浏览器**里会被判成 bot（实测 Error 600010 / widget already executing）。
  真人浏览器则完全正常。

  这个服务在 VPS 上常驻一个「有头 + 持久化 profile + 干净出口 IP」的浏览器，
  青龙或主脚本通过 HTTP 让它去跑挑战，拿到 Turnstile token 或直接完成签到。

  关键点（都来自实测）：
    - 必须 headless=False + Xvfb，headless 一定过不了。
    - 必须持久化 profile，指纹稳定，CF 不会每次当新设备。
    - 引擎优先 camoufox（Firefox 定制版），Chromium 的 SwiftShader 特征会被认出来。
    - Turnstile 参数必须与站点前端一致（action=points_checkin 等）。

接口：
    GET  /health                     健康检查
    POST /turnstile                  取 Turnstile token
    POST /checkin                    直接在浏览器里完成签到（返回结果）

鉴权：环境变量 AGENT_TOKEN 设置后，除 /health 外都需要
      Authorization: Bearer <token>
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict

from flask import Flask, jsonify, request

# browser_engine 只在项目根目录维护一份（agent/ 下不再放副本）。
# 这里按「同目录 → 上一级」的顺序找，兼容两种部署：
#   - systemd：$APP_DIR/agent/panstar_agent.py → 上一级 $APP_DIR/browser_engine.py
#   - docker ：/app/panstar_agent.py        → 同目录 /app/browser_engine.py
# （以前这里是两份各改各的副本，已经出现过版本脱节。）
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _candidate in (_HERE, _PARENT):
    if os.path.exists(os.path.join(_candidate, "browser_engine.py")):
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)
        break

from browser_engine import browser_page

app = Flask(__name__)

AGENT_TOKEN = os.getenv("AGENT_TOKEN", "").strip()
AGENT_HOST = os.getenv("AGENT_HOST", "0.0.0.0")
AGENT_PORT = int(os.getenv("AGENT_PORT", "3000"))
HEADLESS = os.getenv("AGENT_HEADLESS", "false").strip().lower() in ("1", "true", "yes")
ENGINE = os.getenv("AGENT_ENGINE", "auto")
PROXY = os.getenv("AGENT_PROXY", "").strip() or None
PROFILE_DIR = os.getenv("AGENT_PROFILE_DIR", "/app/data/profiles")
DOMAIN = os.getenv("PS_DOMAIN", "https://panstar.ai").rstrip("/")

CHECKIN_SITE_KEY = os.getenv("PS_TURNSITE_SITE_KEY", "0x4AAAAAAD4S6pSwBl0NuILe")
CHECKIN_ACTION = "points_checkin"

# 同一时间只跑一个浏览器任务（浏览器很吃内存）
_lock = threading.Lock()

# 当前是否有浏览器任务在跑。主脚本靠这个判断
# 「要不要在本机也起一个浏览器」—— 小内存机器上两个一起跑会 swap 抖动卡死。
_BUSY = threading.Event()

# 小内存机器上，锁等待时间要短。宁可快速失败让主脚本走别的路，
# 也不要干等着把机器拖垮。
LOCK_WAIT = int(os.getenv("AGENT_LOCK_WAIT", "120"))


def _busy_state() -> Dict[str, Any]:
    return {
        "busy": _BUSY.is_set(),
        "lock_wait": LOCK_WAIT,
    }

app.logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


def auth_ok() -> bool:
    if not AGENT_TOKEN:
        return True  # 没配 token 就放开（仅限内网访问）
    got = request.headers.get("Authorization", "")
    if got.lower().startswith("bearer "):
        got = got[7:].strip()
    return got == AGENT_TOKEN


def require_auth():
    if not auth_ok():
        return jsonify({"ok": False, "error": "UNAUTHORIZED",
                        "message": "token 不正确"}), 401
    return None


# --------------------------------------------------------------------------
# Turnstile：在页面里跑一次挑战
# --------------------------------------------------------------------------
TS_SCRIPT = """
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
        try { w.execute(); } catch (e) { out.err = 'exec:' + e.message; done(); }
      } catch (e) { out.err = 'catch:' + e.message; done(); }
    };
    s.onerror = () => { out.err = 'script-load-failed'; done(); };
    document.head.appendChild(s);
    setTimeout(() => { out.err = out.err || 'timeout'; done(); }, 60000);
  });
}
"""


def _grab_turnstile(page, site_key: str, action: str, page_url: str,
                    timeout_ms: int = 90000) -> str:
    """在已打开的页面里跑 Turnstile，返回 token。"""
    page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
    time.sleep(5)
    result = page.evaluate(TS_SCRIPT, {"siteKey": site_key, "action": action})
    token = (result or {}).get("token")
    if not token:
        raise RuntimeError(f"Turnstile 未返回 token: {(result or {}).get('err')}")
    return token


@app.get("/health")
def health():
    from browser_engine import resolve_engine
    state = _busy_state()
    return jsonify({
        "ok": True,
        "service": "panstar-browser-agent",
        "version": "1.0.0",
        "engine": resolve_engine(ENGINE),
        "headless": HEADLESS,
        "proxy": bool(PROXY),
        "domain": DOMAIN,
        "busy": state["busy"],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/auth-check")
def auth_check():
    """只校验 token，不启动浏览器。

    /health 不鉴权，所以「agent 在线」并不代表「token 是对的」。
    主脚本用这个端点确认两边的 LOGIN_AGENT_TOKEN 一致，
    免得真到签到时才发现 401。
    """
    if not auth_ok():
        return jsonify({"ok": False, "error": "UNAUTHORIZED",
                        "message": "token 不正确"}), 401
    return jsonify({"ok": True, "message": "token 正确"})


@app.post("/turnstile")
def api_turnstile():
    """在浏览器里跑一次 Turnstile，返回 token。

    请求体：{"siteKey": ..., "url": ..., "action": ..., "profile": "可选的profile名"}
    """
    denied = require_auth()
    if denied:
        return denied

    body = request.get_json(silent=True) or {}
    site_key = (body.get("siteKey") or CHECKIN_SITE_KEY).strip()
    url = (body.get("url") or f"{DOMAIN}/console/dashboard").strip()
    action = (body.get("action") or CHECKIN_ACTION).strip()
    profile_name = (body.get("profile") or "default").strip()

    if not _lock.acquire(timeout=LOCK_WAIT):
        return jsonify({"ok": False, "error": "BUSY",
                        "message": "浏览器忙，请稍后重试"}), 429

    _BUSY.set()
    started = time.time()
    try:
        # 用 os.path.join 而不是手拼路径 —— Windows 上斜杠/反斜杠混用会出问题
        profile = os.path.join(PROFILE_DIR, profile_name)
        print(f"[turnstile] 开始 siteKey={site_key} profile={profile}", flush=True)
        with browser_page(
            engine=ENGINE, headless=HEADLESS,
            user_data_dir=profile, proxy=PROXY,
        ) as page:
            token = _grab_turnstile(page, site_key, action, url)
        cost = round(time.time() - started, 1)
        print(f"[turnstile] 成功 用时 {cost}s token={token[:40]}…", flush=True)
        return jsonify({"ok": True, "token": token, "cost": cost})
    except Exception as exc:  # noqa: BLE001
        print(f"[turnstile] 失败 {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"ok": False, "error": "INTERNAL",
                        "message": f"{type(exc).__name__}: {exc}"}), 500
    finally:
        _BUSY.clear()
        _lock.release()


@app.post("/checkin")
def api_checkin():
    """在浏览器里直接完成签到（走站点自己的前端逻辑，最接近真人操作）。

    请求体：{"accessToken": ..., "refreshToken": ..., "profile": "可选"}
    返回：签到结果与积分信息。
    """
    denied = require_auth()
    if denied:
        return denied

    body = request.get_json(silent=True) or {}
    access_token = (body.get("accessToken") or "").strip()
    refresh_token = (body.get("refreshToken") or "").strip()
    profile_name = (body.get("profile") or "default").strip()
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()

    if not access_token and not (username and password):
        return jsonify({"ok": False, "error": "BAD_REQUEST",
                        "message": "需要 accessToken 或 username/password"}), 400

    if not _lock.acquire(timeout=LOCK_WAIT):
        return jsonify({"ok": False, "error": "BUSY",
                        "message": "浏览器忙，请稍后重试"}), 429

    _BUSY.set()
    started = time.time()
    try:
        profile = os.path.join(PROFILE_DIR, profile_name)
        print(f"[checkin] 开始 profile={profile}", flush=True)
        with browser_page(
            engine=ENGINE, headless=HEADLESS,
            user_data_dir=profile, proxy=PROXY,
        ) as page:
            # 注入令牌（前端从 localStorage 读）
            if access_token:
                page.add_init_script(
                    """
                    (a, r) => {
                      try {
                        localStorage.setItem('accessToken', a);
                        if (r) localStorage.setItem('refreshToken', r);
                      } catch (e) {}
                    }
                    """,
                    access_token,
                    refresh_token,
                )

            result = _do_browser_checkin(page, username, password)
        cost = round(time.time() - started, 1)
        print(f"[checkin] 结束 用时 {cost}s 结果={result}", flush=True)
        return jsonify({"ok": True, "result": result, "cost": cost})
    except Exception as exc:  # noqa: BLE001
        print(f"[checkin] 失败 {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"ok": False, "error": "INTERNAL",
                        "message": f"{type(exc).__name__}: {exc}"}), 500
    finally:
        _BUSY.clear()
        _lock.release()


def _do_browser_checkin(page, username: str, password: str) -> Dict[str, Any]:
    """在浏览器里走一遍签到流程。

    站点前端步骤：
      1. 打开 /console/dashboard
      2. 点 "Daily Check-In" 打开弹窗
      3. 点 "Check In Now" → 前端自动跑 Turnstile → preflight → 带 ticket 签到
    """
    page.goto(f"{DOMAIN}/console/dashboard", wait_until="domcontentloaded",
              timeout=90000)
    time.sleep(8)

    if "/login" in page.url and username and password:
        print("[checkin] 需要登录，走登录表单", flush=True)
        page.evaluate(
            """
            (u, p) => {
              const ins = [...document.querySelectorAll('input:not([type=hidden])')];
              const em = ins.find(i => i.type === 'email' || /email|账号/i.test(i.placeholder || ''));
              const pw = ins.find(i => i.type === 'password');
              if (!em || !pw) return false;
              const set = (el, v) => {
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              };
              set(em, u); set(pw, p);
              return true;
            }
            """,
            username, password,
        )
        page.evaluate(
            """
            () => {
              const b = [...document.querySelectorAll('button')]
                .find(e => /^(Log In|登录)$/i.test((e.innerText || '').trim()));
              if (b) b.click();
            }
            """
        )
        for _ in range(30):
            time.sleep(2)
            if "/login" not in page.url:
                break
        time.sleep(5)

    # 打开签到弹窗
    page.evaluate(
        """
        () => {
          const el = [...document.querySelectorAll('button,a,[role="button"]')]
            .find(e => /Daily Check-?In|每日签到/i.test(e.innerText || ''));
          if (el) el.click();
        }
        """
    )
    time.sleep(6)

    # 点 "Check In Now"
    page.evaluate(
        """
        () => {
          const el = [...document.querySelectorAll('button,a,[role="button"]')]
            .find(e => /Check In Now|立即签到|马上签到/i.test(e.innerText || ''));
          if (el) el.click();
        }
        """
    )

    # 等结果
    flow = ""
    for _ in range(40):
        time.sleep(2)
        flow = page.evaluate(
            "() => document.querySelector('[data-checkin-flow-state]')"
            "?.getAttribute('data-checkin-flow-state') || ''"
        )
        if flow in ("SUCCESS", "CHECKED_IN", "ALREADY", "FAILED"):
            break

    dialog = page.evaluate(
        """
        () => ([...document.querySelectorAll('[role="dialog"]')]
          .map(e => (e.innerText || '').replace(/\\s+/g, ' ').slice(0, 400))[0]) || ''
        """
    )
    return {"flow": flow, "dialog": dialog}


if __name__ == "__main__":
    # 浏览器启动失败时残留的 asyncio 任务会打印 "Task was destroyed..." 噪音，
    # 静音它们，免得淹没真正的错误信息。
    import logging
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    logging.getLogger("playwright").setLevel(logging.CRITICAL)

    os.makedirs(PROFILE_DIR, exist_ok=True)
    print(f"[agent] 启动于 {AGENT_HOST}:{AGENT_PORT} engine={ENGINE} "
          f"headless={HEADLESS}", flush=True)
    if not AGENT_TOKEN:
        print("[agent] ⚠️ 未设置 AGENT_TOKEN，任何人都能调用本服务（仅限内网）",
              flush=True)
    app.run(host=AGENT_HOST, port=AGENT_PORT, threaded=False)
