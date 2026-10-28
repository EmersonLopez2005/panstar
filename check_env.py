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


def check_memory() -> None:
    hr("【2.5】内存与 CPU（小内存机器必看）")
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            info = {}
            for line in fh:
                if ":" in line:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0]) // 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        swap_total = info.get("SwapTotal", 0)
        swap_free = info.get("SwapFree", 0)
        print(f"  内存: {avail} MB 可用 / {total} MB 总计")
        print(f"  Swap: {swap_total - swap_free} MB 已用 / {swap_total} MB 总计")

        try:
            cores = os.cpu_count() or 1
        except Exception:
            cores = 1
        print(f"  CPU 核心: {cores}")

        # Camoufox（Firefox 定制版）单个实例常驻 300-500MB。
        # 1 核 / 1GB 的机器上跑浏览器签到非常勉强，
        # 一旦同时起两个浏览器实例就会 swap 抖动 —— CPU 100%、机器卡死。
        if total and total < 1200:
            print()
            print("  ⚠️ 内存偏小（< 1.2GB），浏览器签到很可能失败或把机器拖死")
            print("     建议：")
            print("       - 换台内存 ≥ 2GB 的 VPS（推荐）")
            print("       - 或加 2GB swap：")
            print("         fallocate -l 2G /swapfile && chmod 600 /swapfile && \\")
            print("         mkswap /swapfile && swapon /swapfile")
            print("       - 或人工在浏览器里签到一次，脚本只查状态")
        if cores and cores < 2:
            print()
            print("  ⚠️ 单核机器：浏览器操作会明显变慢，任务超时时间要放宽")
        if swap_total and (swap_total - swap_free) > swap_total * 0.5:
            print()
            print("  ⚠️ Swap 已用超过一半 —— 内存已经不够用了")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 读不到内存信息: {exc}")


def check_disk() -> None:
    hr("【3】磁盘空间")
    try:
        st = os.statvfs(os.path.dirname(os.path.abspath(__file__)))
        avail_mb = st.f_bavail * st.f_frsize / 1024 / 1024
        total_mb = st.f_blocks * st.f_frsize / 1024 / 1024
        print(f"  可用: {avail_mb:.0f} MB / 总计 {total_mb:.0f} MB")
        # Camoufox：下载包 ~1.2GB + 解压后 ~1.2GB 同时存在 → 峰值约 2.6GB
        need = 3072
        if avail_mb < need:
            print(f"  ❌ 空间不足！Camoufox 峰值需要约 2.6GB（下载包+解压同时存在）")
            print("     定位大头：bash cleanup_disk.sh --clean  （会自动扫全盘 Top 15）")
            print("     换大盘放内核：bash install_vps.sh --cache=/mnt/你的大盘")
            print("     轻量替代：bash install_vps.sh --engine=patchright")
            print("     省字体：  bash install_vps.sh --no-cjk-fonts")
        else:
            print(f"  ✅ 空间充足（建议至少 {need} MB）")

        # 浏览器缓存在哪、占了多少
        xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        cam = os.path.join(xdg, "camoufox")
        if os.path.isdir(cam):
            try:
                size = subprocess.run(
                    ["du", "-sh", cam], capture_output=True, text=True, timeout=30
                ).stdout.split()[0]
            except Exception:
                size = "?"
            print(f"  浏览器缓存: {cam} ({size})")
            if os.path.islink(cam):
                print(f"    ↳ 指向大盘: {os.path.realpath(cam)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 读不到磁盘信息: {exc}")


def check_browser(skip: bool = False) -> None:
    hr("【4】浏览器")
    if skip:
        print("  (已跳过)")
        return

    print(f"  Xvfb: {'✅ ' + shutil.which('Xvfb') if shutil.which('Xvfb') else '❌ 未安装（必装，headless 过不了 CF）'}")

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from browser_engine import engine_status, missing_browser_hint, resolve_engine

        status = engine_status()
        print("\n  引擎状态（包已装 ≠ 浏览器已下载，两个都要有）：")
        any_ok = False
        for name, info in status.items():
            pkg = "✅" if info["package"] else "❌"
            binary = "✅" if info["binary"] else "❌"
            print(f"    {name:12} 包={pkg}  浏览器本体={binary}")
            if info["package"] and info["binary"]:
                any_ok = True

        if not any_ok:
            print("\n  ❌ 没有可用的引擎")
            print("  " + missing_browser_hint().replace("\n", "\n  "))
            return

        engine = resolve_engine()
        print(f"\n  将使用: {engine}")
        print("\n  实测挑战（起一个浏览器试跑 Turnstile）…")
        try:
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
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 检查引擎失败: {exc}")


def check_site() -> None:
    hr("【5】站点连通")
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
    check_memory()
    check_disk()
    check_browser(skip=skip_browser)
    check_site()
    hr("体检结束")
    print("  下一步：编辑 .env 填账号，然后 bash run_sign.sh --now")
    return 0


if __name__ == "__main__":
    sys.exit(main())
