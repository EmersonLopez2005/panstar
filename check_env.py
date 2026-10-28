#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部署体检：看看这台机器能不能过 Cloudflare。

要回答三个问题：
  1. 出口 IP 是什么？机房 IP 很可能过不了整页挑战。
  2. 浏览器引擎装好了吗？headless 一定过不了，必须有头 + Xvfb。
  3. 站点能不能连通？

用法：
  python check_env.py              # 完整体检
  python check_env.py --skip-browser   # 只查系统层面
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys


def hr(title: str) -> None:
    print(f"\n{'=' * 58}")
    print(title)
    print("=" * 58)


def check_system() -> None:
    hr("【1】系统")
    print(f"  系统: {platform.system()} {platform.release()}")
    print(f"  架构: {platform.machine()}")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  用户: {os.getenv('USER') or os.getenv('LOGNAME') or 'unknown'}")


def check_network() -> None:
    hr("【2】出口 IP")
    try:
        import requests
        for url in ("https://api.ipify.org?format=json",
                    "https://ipinfo.io/json"):
            try:
                resp = requests.get(url, timeout=10)
                data = resp.json()
                ip = data.get("ip") or data.get("origin")
                org = data.get("org") or data.get("country")
                print(f"  出口 IP: {ip}")
                if data.get("city"):
                    print(f"  位置:   {data.get('city')}, {data.get('country')}")
                if org:
                    print(f"  归属:   {org}")
                break
            except Exception:
                continue
        else:
            print("  ⚠️ 查不到出口 IP")
    except ImportError:
        print("  ⚠️ 缺 requests，跳过")

    print("\n  ⚠️ 关键：机房 IP（阿里云/腾讯云/AWS 等）大概率过不了整页挑战。")
    print("     如果签到一直失败，先查这条。")


def check_browser(skip: bool = False) -> None:
    hr("【3】浏览器")
    if skip:
        print("  (已跳过)")
        return

    print(f"  Xvfb: {'✅ ' + shutil.which('Xvfb') if shutil.which('Xvfb') else '❌ 未安装（必装，headless 过不了 CF）'}")

    camoufox_ok = False
    try:
        import camoufox  # noqa: F401
        camoufox_ok = True
        print("  Camoufox: ✅ 已安装（推荐）")
    except ImportError:
        print("  Camoufox: ❌ 未安装（pip install camoufox[geoip] && python -m camoufox fetch）")

    patchright_ok = False
    try:
        import patchright  # noqa: F401
        patchright_ok = True
        print("  Patchright: ✅ 已安装（备选，成功率较低）")
    except ImportError:
        print("  Patchright: ❌ 未安装")

    if not (camoufox_ok or patchright_ok):
        print("\n  ❌ 没有任何浏览器引擎，签到无法进行。跑 install_vps.sh 装一下。")
        return

    print("\n  实测挑战（起一个浏览器试跑 Turnstile）…")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from browser_engine import browser_page, resolve_engine

        engine = resolve_engine()
        print(f"  引擎: {engine}")
        with browser_page(engine=engine, headless=False,
                          user_data_dir=os.path.join(
                              os.path.dirname(os.path.abspath(__file__)),
                              "data", "check-profile")) as page:
            page.goto("https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            ok = page.evaluate("() => typeof window.turnstile === 'object'")
            print(f"  Turnstile API 加载: {'✅' if ok else '❌'}")
            if ok:
                print("  ✅ 这台机器的环境基本可用")
            else:
                print("  ⚠️ Turnstile API 没加载出来，检查网络/代理")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 实测失败: {type(exc).__name__}: {exc}")


def check_site() -> None:
    hr("【4】站点连通")
    try:
        import requests
        domain = os.environ.get("PS_DOMAIN", "https://panstar.ai")
        resp = requests.get(f"{domain}/api/ecs/auth/turnstile-config",
                            headers={"Accept": "application/json"},
                            timeout=20)
        print(f"  {domain}/api/ecs/auth/turnstile-config → HTTP {resp.status_code}")
        if resp.status_code == 200:
            print("  ✅ 站点可达，接口正常")
        else:
            print("  ⚠️ 状态异常，检查域名/网络")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 请求失败: {exc}")


def main() -> int:
    skip_browser = "--skip-browser" in sys.argv
    check_system()
    check_network()
    check_browser(skip=skip_browser)
    check_site()
    hr("体检结束")
    print("  下一步：编辑 .env 填账号，然后 bash run_sign.sh --now")
    return 0


if __name__ == "__main__":
    sys.exit(main())
