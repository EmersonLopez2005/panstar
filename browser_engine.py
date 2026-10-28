# -*- coding: utf-8 -*-
"""
浏览器引擎适配层（panstar 版）。

与 NodeSeek 版的关键差异：
  - panstar 的 Turnstile 是 invisible + execute 模式，
    token 通过 callback 返回（response-field=false，没有隐藏 input）。
  - 站点前端组件参数（必须一致，否则服务端可能判 token 无效）：
        sitekey  = 0x4AAAAAAD4S6pSwBl0NuILe
        action   = points_checkin
        execution = execute
        invisible = true
        appearance = execute
        retry = never
        refresh-expired = never

引擎优先级：camoufox > patchright > playwright
  - camoufox  : Firefox 定制版，指纹伪装强，过 CF 最稳（推荐）
  - patchright: Playwright 的反检测补丁版，Chromium
  - playwright: 官方版，兜底
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from typing import Optional

DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
DEFAULT_LOCALE = "zh-CN"
DEFAULT_TZ = "Asia/Shanghai"


# --------------------------------------------------------------------------
# 虚拟显示（Linux 有头模式必需）
# --------------------------------------------------------------------------
def _ensure_display() -> Optional[subprocess.Popen]:
    """Linux 上没有 DISPLAY 时自动拉起 Xvfb。"""
    if os.environ.get("DISPLAY"):
        return None
    if shutil.which("Xvfb") is None:
        return None
    display = os.environ.get("PS_DISPLAY_NUM", ":99")
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = display
    time.sleep(1.5)
    return proc


def _has_camoufox() -> bool:
    try:
        import camoufox  # noqa: F401
        return True
    except ImportError:
        return False


def _has_patchright() -> bool:
    try:
        import patchright  # noqa: F401
        return True
    except ImportError:
        return False


def resolve_engine(preferred: Optional[str] = None) -> str:
    """决定用哪个浏览器引擎。"""
    pref = (preferred or os.getenv("PS_ENGINE") or "auto").strip().lower()
    if pref in ("camoufox", "patchright", "playwright"):
        return pref
    if _has_camoufox():
        return "camoufox"
    if _has_patchright():
        return "patchright"
    return "playwright"


def parse_proxy(proxy: Optional[str]) -> Optional[dict]:
    """把 http://user:pass@host:port 解析成 playwright 的 proxy 配置。"""
    if not proxy:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(proxy)
    if not parsed.hostname:
        return None
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


@contextmanager
def browser_page(
    engine: str = "auto",
    headless: bool = False,
    user_data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
    locale: str = DEFAULT_LOCALE,
    timezone: str = DEFAULT_TZ,
    slow_mo: int = 0,
    humanize: bool = True,
):
    """打开一个浏览器页面，yield 出 playwright 的 Page 对象。

    用 user_data_dir 时是持久化 profile —— 指纹稳定，
    Cloudflare 不会每次都把当新设备重新挑战。
    """
    name = resolve_engine(engine)
    proxy_cfg = parse_proxy(proxy)

    if user_data_dir:
        os.makedirs(user_data_dir, exist_ok=True)

    xvfb_proc = None
    if not headless:
        xvfb_proc = _ensure_display()

    try:
        if name == "camoufox":
            with _camoufox_page(headless, user_data_dir, proxy_cfg, locale, humanize) as page:
                yield page
        else:
            with _playwright_page(
                name, headless, user_data_dir, proxy_cfg, locale, timezone, slow_mo
            ) as page:
                yield page
    finally:
        if xvfb_proc is not None:
            try:
                xvfb_proc.terminate()
            except Exception:
                pass
            os.environ.pop("DISPLAY", None)


@contextmanager
def _camoufox_page(headless, user_data_dir, proxy_cfg, locale, humanize):
    from camoufox.sync_api import Camoufox

    kwargs = dict(headless=headless, humanize=humanize, locale=locale)
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    if user_data_dir:
        kwargs["user_data_dir"] = user_data_dir
        kwargs["persistent_context"] = True

    with Camoufox(**kwargs) as browser:
        if user_data_dir:
            # persistent_context 模式：browser 本身就是 context
            pages = browser.pages
            page = pages[0] if pages else browser.new_page()
            try:
                yield page
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        else:
            context = browser.new_context()
            page = context.new_page()
            try:
                yield page
            finally:
                try:
                    context.close()
                except Exception:
                    pass


@contextmanager
def _playwright_page(name, headless, user_data_dir, proxy_cfg, locale, timezone, slow_mo):
    if name == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright

    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1440,900",
        # 注意：不要加 --disable-gpu！
        # Xvfb + --disable-gpu 会让 WebGL 走 SwiftShader，
        # UNMASKED_RENDERER_WEBGL 直接暴露 "Google SwiftShader"，
        # 这是 Cloudflare 已知的 bot 特征（实测触发 Turnstile onError）。
        "--disable-background-networking",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-domain-reliability",
        "--disable-extensions",
        "--disable-features=Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-default-browser-check",
        "--no-first-run",
        "--renderer-process-limit=2",
        "--js-flags=--max-old-space-size=384",
    ]
    ctx_kwargs = dict(
        locale=locale,
        timezone_id=timezone,
        viewport=DEFAULT_VIEWPORT,
        ignore_https_errors=False,
    )

    p = sync_playwright().start()
    try:
        if user_data_dir:
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                args=args,
                proxy=proxy_cfg,
                slow_mo=slow_mo,
                **ctx_kwargs,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                yield page
            finally:
                try:
                    context.close()
                except Exception:
                    pass
        else:
            browser = p.chromium.launch(
                headless=headless, args=args, proxy=proxy_cfg, slow_mo=slow_mo
            )
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            try:
                yield page
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
    finally:
        try:
            p.stop()
        except Exception:
            pass
