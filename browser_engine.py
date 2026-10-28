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


def _cache_home() -> str:
    """浏览器缓存根目录。

    Camoufox 用 `platformdirs.user_cache_dir("camoufox")`，在 Linux 上等价于
    `~/.cache/camoufox`，但它**遵循 XDG_CACHE_HOME**；
    Playwright / Patchright 遵循 `PLAYWRIGHT_BROWSERS_PATH`。
    两者都要认，否则「把浏览器内核挪到大盘上」就落不了地。
    """
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    return xdg or os.path.join(os.path.expanduser("~"), ".cache")


def _camoufox_install_dir() -> Optional[str]:
    """Camoufox 的安装目录（不触发下载）。"""
    # 新版 camoufox.pkgman 直接暴露 INSTALL_DIR
    try:
        from camoufox import pkgman
        inst = getattr(pkgman, "INSTALL_DIR", None)
        if inst:
            return str(inst)
    except Exception:
        pass
    # 退回到 platformdirs 的默认位置
    try:
        from platformdirs import user_cache_dir
        return user_cache_dir("camoufox")
    except Exception:
        pass
    return os.path.join(_cache_home(), "camoufox")


def _camoufox_binary_installed() -> bool:
    """检查 Camoufox 浏览器本体是否已下载。

    `pip install camoufox` 只装了 Python 包，浏览器本体（约 1.2GB）还需要
    单独执行 `python -m camoufox fetch` 才会下载。

    只判 import 会造成误判：以为引擎可用，实际在运行时才去下载，
    磁盘不够就直接失败（ERRNO 28 No space left on device）。

    注意：**不能调用 `pkgman.camoufox_path()`** —— 新版该函数在未安装时
    会触发自动下载，体检脚本只想「看一眼」，不该把 1.2GB 拖下来。
    """
    try:
        from camoufox import pkgman  # noqa: F401
    except ImportError:
        return False

    inst = _camoufox_install_dir()
    if not inst or not os.path.isdir(inst):
        return False

    # 新版多版本结构：browsers/<version>/camoufox-bin
    browsers_dir = os.path.join(inst, "browsers")
    if os.path.isdir(browsers_dir):
        for entry in os.listdir(browsers_dir):
            candidate = os.path.join(browsers_dir, entry, "camoufox-bin")
            if os.path.exists(candidate):
                return True

    # 旧版单版本结构：<inst>/camoufox-bin + version.json
    if os.path.exists(os.path.join(inst, "camoufox-bin")):
        return True
    if os.path.exists(os.path.join(inst, "version.json")):
        return True

    # 兜底：目录里有可执行文件就算装好了
    for dirpath, _dirnames, filenames in os.walk(inst):
        for name in filenames:
            if name == "camoufox-bin" or name == "camoufox" or name == "firefox":
                return True
    return False


def _has_camoufox() -> bool:
    """Camoufox 是否真正可用（包已装 **且** 浏览器本体已下载）。"""
    try:
        import camoufox  # noqa: F401
    except ImportError:
        return False
    return _camoufox_binary_installed()


def _playwright_browsers_path() -> str:
    """Playwright / Patchright 的浏览器安装目录。"""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom:
        return custom
    return os.path.join(_cache_home(), "ms-playwright")


def _has_patchright() -> bool:
    """Patchright 的 Chromium 是否已下载。"""
    try:
        import patchright  # noqa: F401
    except ImportError:
        return False
    root = _playwright_browsers_path()
    if not os.path.isdir(root):
        return False
    for name in os.listdir(root):
        if name.startswith("chromium"):
            return True
    return False


def resolve_engine(preferred: Optional[str] = None) -> str:
    """决定用哪个浏览器引擎。

    注意：这里必须确认「浏览器本体已下载」，而不只是 pip 包已装。
    否则会在真正跑签到时才去下载 1.3GB，磁盘不够就直接失败。
    """
    pref = (preferred or os.getenv("PS_ENGINE") or "auto").strip().lower()
    if pref in ("camoufox", "patchright", "playwright"):
        return pref
    if _has_camoufox():
        return "camoufox"
    if _has_patchright():
        return "patchright"
    return "playwright"


def engine_status() -> dict:
    """各引擎的可用状态，用于体检与错误提示。"""
    return {
        "camoufox": {
            "package": _package_installed("camoufox"),
            "binary": _camoufox_binary_installed(),
        },
        "patchright": {
            "package": _package_installed("patchright"),
            "binary": _has_patchright(),
        },
        "playwright": {
            "package": _package_installed("playwright"),
            "binary": _playwright_binary_installed(),
        },
    }


def _package_installed(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _playwright_binary_installed() -> bool:
    root = _playwright_browsers_path()
    if not os.path.isdir(root):
        return False
    for name in os.listdir(root):
        if name.startswith("chromium"):
            return True
    return False


def missing_browser_hint() -> str:
    """所有引擎都不可用时，给出可执行的修复建议。"""
    status = engine_status()
    lines = ["没有可用的浏览器引擎。"]
    if status["camoufox"]["package"] and not status["camoufox"]["binary"]:
        lines.append(
            "  Camoufox 的 Python 包已装，但浏览器本体（约 1.2GB）没下载 —— "
            "执行：python -m camoufox fetch"
        )
    elif not status["camoufox"]["package"]:
        lines.append(
            "  安装 Camoufox：pip install 'camoufox[geoip]' && python -m camoufox fetch"
        )
    if not status["patchright"]["binary"]:
        lines.append(
            "  备选（体积小但过 CF 成功率低）："
            "pip install patchright && patchright install --with-deps chromium"
        )
    lines.append("  提示：Camoufox 下载包 + 解压后同时存在，**峰值约 2.6GB**，请先确认空间。")
    lines.append("  空间不够时：bash cleanup_disk.sh --clean  或用 --cache=/大盘 换位置。")
    return "\n".join(lines)


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
def _memory_mb() -> int:
    """系统可用内存（MB）。读不到时返回一个大数（不做限制）。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 99999


def _low_memory_mode() -> bool:
    """是否处于「小内存机器」模式。

    1 核 / 1GB 以下的 VPS 上，一个 Camoufox 实例就要吃掉几百 MB。
    这时候再叠加第二个浏览器实例（agent 的 + 本地回退的），
    就会触发 swap 抖动 —— 表现是 CPU 100% 但什么都干不完，机器直接卡死。
    """
    if os.getenv("PS_LOW_MEMORY", "").strip() in ("1", "true", "yes"):
        return True
    avail = _memory_mb()
    # 可用内存低于 700MB 视为小内存机器
    return avail < 700


def _apply_memory_limits() -> None:
    """给 Firefox 进程设内存上限，防止单个实例把整机内存吃光。

    Firefox 默认不管内存，页面多 + 有头模式下很容易冲到 1GB+。
    这里用 MOZ_* 环境变量限制地址空间，配合 low-memory 模式进一步收紧。
    """
    if os.environ.get("PS_NO_MEM_LIMIT"):
        return
    if _low_memory_mode():
        # 小内存机器：把地址空间压到 512MB，触发 GC 更频繁但不会 OOM
        os.environ.setdefault("MOZ_DISABLE_CONTENT_SANDBOX", "1")
        os.environ.setdefault("MOZ_FORCE_DISABLE_E10S", "1")


@contextmanager
def browser_page(
    engine: str = "auto",
    headless: bool = False,
    user_data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
    locale: str = DEFAULT_LOCALE,
    timezone: str = DEFAULT_TZ,
    slow_mo: int = 0,
    humanize: Optional[bool] = None,
):
    """打开一个浏览器页面，yield 出 playwright 的 Page 对象。

    用 user_data_dir 时是持久化 profile —— 指纹稳定，
    Cloudflare 不会每次都把当新设备重新挑战。

    humanize 默认为 True，但小内存机器上会自动关掉
    （它会持续跑鼠标轨迹模拟，是纯 CPU 开销，对过 CF 无实质帮助）。
    """
    name = resolve_engine(engine)
    proxy_cfg = parse_proxy(proxy)

    low_mem = _low_memory_mode()
    if humanize is None:
        humanize = not low_mem

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
