#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
签到环境诊断 —— 把「配置到底读成了什么」一次说清楚。

为什么需要这个：
    之前遇到过 .env 里 `LOGIN_AGENT_TOKEN=`（空值，.env.example 自带的一行）
    排在真正填入的值前面，加载器「先到先得」→ 实际生效的是空字符串 →
    agent 一直返回 401「token 不正确」。
    光看 .env 文件内容完全看不出来，因为文件里明明有值。

    这个脚本直接打印**运行时真正读到的值**，一眼就能定位。

用法：
    python3 check_config.py
"""

from __future__ import annotations

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

ENV_FILE = os.path.join(APP_DIR, ".env")


def show_raw() -> None:
    """列出 .env 里所有相关键的原始行，看有没有重复。"""
    print("=" * 62)
    print("【1】.env 原始内容（相关键）")
    print("=" * 62)
    if not os.path.exists(ENV_FILE):
        print(f"  ⚠️ .env 不存在: {ENV_FILE}")
        return
    keys = ("LOGIN_AGENT_URL", "LOGIN_AGENT_TOKEN", "PS_USER_PASS",
            "PS_USER", "PS_PASS", "PS_TOTP", "XDG_CACHE_HOME", "TMPDIR")
    found = {}
    with open(ENV_FILE, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key in keys:
                value = line.split("=", 1)[1].strip()
                # 不打印密码明文
                shown = value
                if key in ("PS_USER_PASS", "PS_PASS"):
                    shown = (value[:6] + "…" + value[-2:]) if len(value) > 10 else "…"
                if key == "LOGIN_AGENT_TOKEN":
                    shown = (value[:8] + "…" + value[-4:]) if len(value) > 14 else ("(空)" if not value else "…")
                found.setdefault(key, []).append((lineno, shown))
    for key in keys:
        if key in found:
            for lineno, shown in found[key]:
                mark = "  ⚠️ 重复！" if len(found[key]) > 1 else ""
                print(f"  第{lineno:3d}行  {key:20} = {shown}{mark}")
            if len(found[key]) > 1:
                print(f"         ↳ {key} 出现 {len(found[key])} 次 —— 旧的加载器只认第一处，"
                      "后面的会被静默丢弃")
        else:
            print(f"           {key:20} = (未设置)")


def _parse_env(path: str) -> dict:
    """独立解析 .env —— 不依赖 panstar_sign，避免拖进 requests/cryptography 等依赖。

    解析规则与 panstar_sign.load_env_file 一致：
    空值不占位，同键最后一个非空值生效，外部环境变量优先。
    """
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or not value:
                continue
            result[key] = value
    return result


def show_effective() -> None:
    """运行时真正读到的值。"""
    print()
    print("=" * 62)
    print("【2】运行时实际生效的值")
    print("=" * 62)
    env = _parse_env(ENV_FILE)

    url = env.get("LOGIN_AGENT_URL", "")
    token = env.get("LOGIN_AGENT_TOKEN", "")
    user = env.get("PS_USER_PASS", "")

    print(f"  LOGIN_AGENT_URL   = {url or '(空)'}")
    print(f"  LOGIN_AGENT_TOKEN = {(token[:8] + '…' + token[-4:]) if token else '(空) ⚠️'}")
    print(f"  PS_USER_PASS      = {(user[:6] + '…') if user else '(空) ⚠️'}")

    if not token:
        print()
        print("  ❌ token 是空的 —— 这就是 401「token 不正确」的原因")
        print("     .env 里可能有重复的 LOGIN_AGENT_TOKEN=，"
              "前面那个空值把真值挡住了。")
        print("     修复：把所有重复行删掉，只留一行有值的：")
        print("       grep -n LOGIN_AGENT_TOKEN .env")
        print("       sed -i '/^LOGIN_AGENT_TOKEN=$/d' .env")
    elif "=" in token:
        # **常见手滑**：把 KEY= 前缀也复制进来了。
        # 例如从 systemd 单元复制时拿的是 `Environment=AGENT_TOKEN=xxx`，
        # 结果填成 `LOGIN_AGENT_TOKEN=AGENT_TOKEN=xxx`。
        print()
        prefix = token.split("=", 1)[0]
        print(f"  ❌ token 的值里混进了 '{prefix}=' 前缀 —— 这就是 401 的原因")
        print(f"     你填的是： {token}")
        print(f"     应该是：   {token.split('=', 1)[1]}")
        print()
        print("     一键修复：")
        print(f"       sed -i 's|^LOGIN_AGENT_TOKEN=.*|LOGIN_AGENT_TOKEN="
              f"{token.split('=', 1)[1]}|' .env")
        print("       systemctl restart panstar-agent")
    if not url:
        print()
        print("  ❌ 没配 LOGIN_AGENT_URL —— 浏览器代理根本不会被调用")
        print("     修复：")
        print("       echo 'LOGIN_AGENT_URL=http://127.0.0.1:3000' >> .env")
        print("     （同样要先删掉重复的空值行）")


def check_agent() -> None:
    """连 agent 测一下。"""
    print()
    print("=" * 62)
    print("【3】浏览器代理连通性")
    print("=" * 62)
    env = _parse_env(ENV_FILE)
    url = env.get("LOGIN_AGENT_URL", "")
    token = env.get("LOGIN_AGENT_TOKEN", "")
    if not url:
        print("  跳过（没配 LOGIN_AGENT_URL）")
        return

    try:
        import requests
    except ImportError:
        print("  跳过（没装 requests）")
        return

    try:
        resp = requests.get(f"{url.rstrip('/')}/health", timeout=8)
        print(f"  /health      → HTTP {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    engine = {data.get('engine')}")
            print(f"    busy   = {data.get('busy')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  /health      → 连接失败: {exc}")
        print("     启动：systemctl start panstar-agent")
        return

    if not token:
        print("  /auth-check  → 跳过（token 为空）")
        return

    try:
        resp = requests.get(f"{url.rstrip('/')}/auth-check",
                            headers={"Authorization": f"Bearer {token}"}, timeout=8)
        if resp.status_code == 200:
            print("  /auth-check  → ✅ token 正确")
        elif resp.status_code == 401:
            print("  /auth-check  → ❌ 401 token 不正确")
            _print_token_mismatch(token)
        elif resp.status_code == 404:
            # agent 版本旧，没有 /auth-check 端点 ——
            # 但不代表 token 对错，改成直接读 systemd 单元对比。
            print("  /auth-check  → HTTP 404（agent 版本旧，没有这个端点）")
            _compare_with_unit(token)
        else:
            print(f"  /auth-check  → HTTP {resp.status_code}")
            _compare_with_unit(token)
    except Exception as exc:  # noqa: BLE001
        print(f"  /auth-check  → 请求失败: {exc}")
        _compare_with_unit(token)


UNIT_PATH = "/etc/systemd/system/panstar-agent.service"


def _unit_token() -> str:
    """从 systemd 单元里读出 AGENT_TOKEN。"""
    try:
        with open(UNIT_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("Environment=AGENT_TOKEN="):
                    return line.split("=", 2)[2].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _compare_with_unit(token: str) -> None:
    """直接把 .env 里的 token 和 systemd 单元里的对比。"""
    unit = _unit_token()
    if not unit:
        print()
        print(f"  读不到 {UNIT_PATH}")
        print("  手动对比：")
        print("    grep AGENT_TOKEN " + UNIT_PATH)
        return

    print()
    print("  token 对比：")
    print(f"    .env 里         = {token[:8]}…{token[-4:]}")
    print(f"    systemd 单元里  = {unit[:8]}…{unit[-4:]}")
    if token == unit:
        print("    ✅ 两边一致")
        return

    print("    ❌ 两边不一致")
    print()
    # 常见的手滑：把 KEY= 前缀也复制进去了
    if "=" in token:
        prefix = token.split("=", 1)[0]
        if unit == token.split("=", 1)[1]:
            print(f"    💡 找到了：你把 '{prefix}=' 也复制进去了！")
            print(f"       正确的值应该是（不要带前缀）：")
            print(f"         {unit}")
            print()
            print("       一键修复：")
            print(f"         sed -i 's|^LOGIN_AGENT_TOKEN=.*|LOGIN_AGENT_TOKEN={unit}|' .env")
            print("         systemctl restart panstar-agent")
            return
    print("    修复（推荐让脚本自动对齐，别手抄）：")
    print("      cd ~/panstar && git pull")
    print("      bash install_vps.sh --agent")
    print("    或手动：")
    print(f"      sed -i 's|^LOGIN_AGENT_TOKEN=.*|LOGIN_AGENT_TOKEN={unit}|' .env")
    print("      systemctl restart panstar-agent")


def _print_token_mismatch(token: str) -> None:
    print()
    print("     两边的 token 必须一致：")
    print("       .env 里：")
    print("         grep LOGIN_AGENT_TOKEN .env")
    print("       agent 服务里：")
    print(f"         grep AGENT_TOKEN {UNIT_PATH}")
    print("     同步后重启： systemctl restart panstar-agent")
    if "=" in token:
        prefix = token.split("=", 1)[0]
        unit = _unit_token()
        if unit and unit == token.split("=", 1)[1]:
            print()
            print(f"     💡 看起来是你把 '{prefix}=' 前缀也复制进去了")
            print(f"        正确的值：{unit}")
            print(f"        一键修复：")
            print(f"          sed -i 's|^LOGIN_AGENT_TOKEN=.*|LOGIN_AGENT_TOKEN={unit}|' .env")
            print("          systemctl restart panstar-agent")


def main() -> int:
    print("=" * 62)
    print("PanstarCloud 签到配置诊断")
    print("=" * 62)
    show_raw()
    show_effective()
    check_agent()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
