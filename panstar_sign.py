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
    """把 .env 里的键值读进 os.environ。

    规则：
      1. 已在环境里的**非空**变量优先，不被 .env 覆盖
         （cron/手动 export 的优先级最高）
      2. **空值不占位** —— 否则 .env 里先出现的 `LOGIN_AGENT_TOKEN=`
         （.env.example 自带的一行）会把后面真正填入的值挡掉，
         导致实际生效的是空字符串 → agent 返回 401「token 不正确」。
         这正是之前踩的坑。
      3. 同一个键出现多次时，**最后一个非空值生效**。

    实现要点：**必须先把文件读完整再写 os.environ**。
    如果边读边写，第一个非空值写进去后，第二个非空值来时
    os.environ 里已经有值了 —— 无法区分「外部环境变量」和
    「同一文件里前面读进来的」，后者会被误判成外部变量而跳过。
    （实测踩过：K=OLD / K=NEW 结果取到了 OLD。）
    """
    if not os.path.exists(path):
        return

    # 第一遍：收集文件里每个键的最后一个非空值
    file_values: Dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or not value:
                continue          # 空值不占位
            file_values[key] = value

    # 第二遍：写进 os.environ（外部已有的非空值优先）
    for key, value in file_values.items():
        existing = os.environ.get(key, "")
        if existing and existing.strip():
            continue              # 外部环境变量优先
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
class AgentAuthError(Exception):
    """浏览器代理鉴权失败（401）。

    单独一个类型，因为它是**配置问题**，和内存、浏览器、CF 都无关。
    之前被笼统地包成 RuntimeError，结果上层把「内存不足、浏览器方案不可行」
    当成结论抛出来 —— 完全是误导，会让人往错的方向排查。
    """


class AgentCrashedError(Exception):
    """浏览器代理进程在请求处理中挂了。

    典型表现：requests 抛
      ('Connection aborted.', RemoteDisconnected('Remote end closed connection...'))
    这不是 Python 异常，是 **HTTP 连接被对端掐断** ——
    说明 agent 进程死了（最常见是被 systemd 按 MemoryMax 杀掉），
    而不是浏览器或 Cloudflare 的问题。

    单独一个类型，是为了把「进程被杀」和「业务失败」区分开：
    前者要去看 journalctl / 调内存上限，后者才要调浏览器逻辑。
    """


def _explain_conn_error(agent_url: str, exc: Exception) -> str:
    """把连接类异常翻译成可执行的排查方向。"""
    raw = str(exc)
    if "RemoteDisconnected" in raw or "Connection aborted" in raw:
        return (
            "浏览器代理进程在处理请求时挂掉了（连接被掐断）。\n"
            "  这不是 Python 异常，也不是浏览器/Cloudflare 的问题 ——\n"
            "  最常见原因是 agent 被 systemd 按内存上限 OOM 杀掉。\n"
            "\n"
            "  确认是不是 OOM：\n"
            f"    systemctl show panstar-agent -p NRestarts\n"
            f"    journalctl -u panstar-agent -n 100 --no-pager | grep -iE 'oom|killed|memory'\n"
            f"    curl -s {agent_url.rstrip('/')}/health   # 看 mem.current_mb / mem.max_mb\n"
            "\n"
            "  修复：重跑安装脚本，它会按物理内存动态计算上限\n"
            "  （旧版写死 MemoryMax=700M，对 Firefox 太小）：\n"
            "    cd ~/panstar && git pull && bash install_vps.sh --agent\n"
            "\n"
            "  应急：临时放宽上限\n"
            "    systemctl set-property panstar-agent MemoryMax=1500M\n"
            "    systemctl restart panstar-agent"
        )
    return f"访问浏览器代理失败：{type(exc).__name__}: {raw}"


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

    try:
        resp = requests.post(
            f"{agent_url.rstrip('/')}/checkin",
            headers=headers,
            json=body,
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError as exc:
        raise AgentCrashedError(_explain_conn_error(agent_url, exc)) from exc
    except requests.exceptions.RequestException as exc:
        raise AgentCrashedError(
            f"访问浏览器代理失败：{type(exc).__name__}: {exc}"
        ) from exc
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"浏览器代理返回非 JSON: {resp.text[:200]}")
    if not data.get("ok"):
        # **401 要单独识别。**
        # agent 的 401 = LOGIN_AGENT_TOKEN 和它自己的 AGENT_TOKEN 对不上。
        # 这是配置问题，不是内存问题、不是浏览器问题。
        # 之前它被笼统地包成 RuntimeError，导致上层把「内存不足、
        # 浏览器方案不可行」当成结论抛出来 —— 完全是误导。
        if resp.status_code == 401 or data.get("error") == "UNAUTHORIZED":
            raise AgentAuthError(
                "浏览器代理鉴权失败（401）：.env 里的 LOGIN_AGENT_TOKEN 与 "
                "agent 服务的 AGENT_TOKEN 不一致。\n"
                "  修复（二选一）：\n"
                "    A) 重新跑安装脚本，它会把 token 统一写进 .env：\n"
                "         bash install_vps.sh --agent\n"
                "    B) 手动同步（注意 .env 里是 LOGIN_AGENT_TOKEN）：\n"
                "         grep AGENT_TOKEN /etc/systemd/system/panstar-agent.service | cut -d= -f3\n"
                "         # 把上面那串（只有值，不要带 AGENT_TOKEN= 前缀）填到 .env\n"
                "       然后 systemctl restart panstar-agent"
            )
        # agent 带了 traceback 就一起带出来 —— 只看一行异常消息无法定位问题
        tb = data.get("traceback") or ""
        msg = data.get("message") or data.get("error") or resp.text[:150]
        if tb:
            raise RuntimeError(f"浏览器代理失败: {msg}\n{tb}")
        raise RuntimeError(
            f"浏览器代理失败: {msg}"
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

    # agent 状态自检：配置了但没起来，要立刻说清楚，
    # 否则用户只会看到"本地浏览器失败"，以为是浏览器的问题。
    if agent_url:
        from turnstile_solver import check_agent_health
        health = check_agent_health(agent_url, agent_token)
        if health["ok"] and not health.get("auth_ok", True):
            # **鉴权失败要立刻报出来。**
            # /health 不鉴权，所以「在线」不代表「token 对」。
            # 如果不拦 here，错误会一路传到「内存不足、浏览器方案不可行」，
            # 把人带偏到完全无关的方向。
            raise AgentAuthError(
                "浏览器代理鉴权失败（401）：.env 里的 LOGIN_AGENT_TOKEN 与 "
                "agent 服务的 AGENT_TOKEN 不一致。\n"
                "  修复（二选一）：\n"
                "    A) 重新跑安装脚本，它会把 token 统一写进 .env：\n"
                "         bash install_vps.sh --agent\n"
                "    B) 手动同步：\n"
                "         grep AGENT_TOKEN /etc/systemd/system/panstar-agent.service\n"
                "         # 把上面那串填到 .env 的 LOGIN_AGENT_TOKEN= 后面\n"
                "       然后 systemctl restart panstar-agent"
            )
        if health["ok"]:
            print(f"[agent] 在线（engine={health['engine']}）")
        else:
            print(f"[agent] ⚠️ 不可用: {health['message']}")
            print("[agent]    启动：systemctl start panstar-agent"
                  "  或  cd agent && docker compose up -d")
    else:
        print("[agent] 未配置 LOGIN_AGENT_URL，将使用本机浏览器")
        print("[agent]    提示：本机浏览器需要约 1.3GB 磁盘（Camoufox），"
              "VPS 空间紧张时更推荐用 agent")

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
    agent_failed = False
    agent_error = ""
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
            dialog = (agent_result.get("dialog") or "").strip()
            print(f"[checkin] 浏览器 flow={flow or '(空)'}"
                  + (f" dialog={dialog[:120]}" if dialog else ""))

            # **以服务端状态为准，不靠 flow 字符串猜。**
            #
            # 前端 data-checkin-flow-state 的终态是 SUCCEEDED / FAILED，
            # 早先这里写的是 ("SUCCESS", "CHECKED_IN", "ALREADY") ——
            # 三个值前端都不存在，于是「签到成功」被当成失败上报。
            #
            # 只修字符串还不够：站点以后改状态名还会再错一次。
            # 直接查今日签到状态，无论前端叫什么，服务端说签了就是签了。
            status = client.checkin_status()
            if status.get("checkedIn"):
                summary = format_summary(status)
                print(f"[checkin] 浏览器签到成功 → {summary}")
                return {"status": "success", "summary": summary}

            if flow == "SUCCEEDED":
                # 前端说成功、服务端说没签到 —— 可能结算有延迟，再查一次
                time.sleep(3)
                status = client.checkin_status()
                if status.get("checkedIn"):
                    summary = format_summary(status)
                    print(f"[checkin] 浏览器签到成功（延迟结算）→ {summary}")
                    return {"status": "success", "summary": summary}
                agent_error = (
                    f"浏览器流程显示成功（flow=SUCCEEDED），"
                    f"但服务端仍显示未签到。dialog={dialog[:200] or '(空)'}"
                )
            else:
                agent_error = (
                    f"浏览器流程未完成（flow={flow or '(空)'}）。"
                    f"dialog={dialog[:200] or '(空)'}"
                )
            print(f"[checkin] {agent_error}")
            agent_failed = True
        except AgentAuthError:
            # 鉴权失败是配置问题，直接抛出 —— 不要再被当成「内存不足」
            raise
        except AgentCrashedError:
            # agent 进程挂了，信息本身已经说清原因和排查步骤，直接抛出
            raise
        except Exception as exc:
            print(f"[checkin] 浏览器代理签到失败: {exc}")
            agent_failed = True
            agent_error = str(exc)

    # 3c. 退一步：让浏览器只拿 Turnstile token，主脚本自己走 preflight → 签到
    #
    # **小内存机器上绝不能在这里再起一个本地浏览器。**
    # agent 刚才已经试过了，失败说明浏览器这条路本身有问题
    # （内存不够 / CF 拦 / 引擎坏了）。此时再起第二个浏览器实例，
    # 在 1 核 967MB 的机器上会直接 swap 抖动 —— 表现为 CPU 100%、
    # 机器卡死、日志一行都出不来。
    # 所以：agent 失败过就直接放弃浏览器路径，别把机器拖垮。
    if result is None:
        if agent_failed:
            # **不要再一律说「内存不足、机器不可行」。**
            # agent 失败的原因绝大多数跟内存无关（配置错、状态值判断错、
            # 页面改版……）。笼统推给「硬件不行」会让人白换机器。
            # 只有确认可用内存真的很低时才提机器规格。
            avail = 0
            try:
                with open("/proc/meminfo", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("MemAvailable:"):
                            avail = int(line.split()[1]) // 1024
                            break
            except Exception:
                pass
            low_mem = 0 < avail < 700
            hint = f"浏览器代理签到失败：{agent_error}\n"
            if low_mem:
                hint += (
                    f"  ⚠️ 当前可用内存仅 {avail} MB（Camoufox 需 300-500MB），"
                    "机器规格可能是根因\n"
                    "  可加 swap：\n"
                    "    fallocate -l 2G /swapfile && chmod 600 /swapfile && "
                    "mkswap /swapfile && swapon /swapfile\n"
                )
            hint += (
                "  排查方向：\n"
                "    1) 浏览器日志：journalctl -u panstar-agent -n 80 --no-pager\n"
                "    2) 内存余量：  curl -s http://127.0.0.1:3000/health\n"
                "    3) 配置自检：  python3 check_config.py\n"
                "    4) 冒烟测试：  python3 smoke_test.py"
            )
            raise PanstarError(718, hint)
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
    # Camoufox / Playwright 在浏览器启动失败时会残留 asyncio 任务，
    # 退出时打印 "Task was destroyed but it is pending!" 之类的噪音。
    # 这些不是错误，只是难看 —— 直接静音，让真实错误不被淹没。
    import logging
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    try:
        from playwright._impl._errors import TargetClosedError
        logging.getLogger("playwright").setLevel(logging.CRITICAL)
    except Exception:
        pass

    sys.exit(main())
