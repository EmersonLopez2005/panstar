#!/usr/bin/env bash
# 每日签到的 cron 包装脚本。
#
# 为什么不让 crontab 直接调 python：
#   1) crontab 的命令行对引号/百分号极其敏感（% 在 crontab 里是换行符）；
#   2) flock 拿不到锁时**静默跳过**，日志里一个字节都没有 ——
#      这是最难查的故障形态（cron 触发了，但脚本没跑，也没日志）；
#   3) 没有心跳，无法区分「cron 根本没触发」和「触发了但脚本崩了」；
#   4) **环境变量不能和调度字段写在同一行** —— cron 解析器遇到 '=' 会把
#      整行当成「环境变量赋值」，这一行一个 job 都不生成，到点永不触发，
#      而且不报任何错。环境变量必须在这里 export，而不是塞进 crontab。
#
# 用法：bash run_sign.sh          （正常跑，输出写进 logs/sign.log）
#       bash run_sign.sh --now    （忽略锁，立刻跑一次）
#       bash run_sign.sh --dryrun （只检查环境与依赖，不实际签到）

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

# ---- 环境：自己补齐，不依赖 crontab 里的变量行 ----
export HOME="${HOME:-/root}"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"

# ---- 从 .env 读浏览器缓存路径 ----
# 用了 --cache=/大盘 安装时，浏览器内核不在 ~/.cache 下。
# cron 环境下没有这些环境变量，不读 .env 就会报「找不到浏览器」。
if [ -f "$APP_DIR/.env" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      XDG_CACHE_HOME|PLAYWRIGHT_BROWSERS_PATH|TMPDIR)
        [ -n "$v" ] && export "$k=$v"
        ;;
    esac
  done < <(grep -E '^(XDG_CACHE_HOME|PLAYWRIGHT_BROWSERS_PATH|TMPDIR)=' "$APP_DIR/.env" 2>/dev/null)
fi

LOG_DIR="$APP_DIR/logs"
LOG_FILE="$LOG_DIR/sign.log"
HEARTBEAT="$LOG_DIR/last_run.txt"
LOCK_FILE="${PS_SIGN_LOCK:-/tmp/panstar-sign.lock}"

mkdir -p "$LOG_DIR"

ts() { date -Is; }

# ---------------------------------------------------------------- 选解释器
PY=""
for c in "$APP_DIR/.venv/bin/python" "$APP_DIR/venv/bin/python" \
         "$(command -v python3 2>/dev/null || true)" \
         "$(command -v python 2>/dev/null || true)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break; fi
done

DRYRUN=0
[ "${1:-}" = "--dryrun" ] && DRYRUN=1

# 预演模式：**不写 sign.log、不写心跳**。
# 它只是给人看的诊断输出，混进签到日志里会让人分不清哪段是预演、哪段是真签到。
if [ "$DRYRUN" != "1" ]; then
  {
    echo ""
    echo "================ [$(ts)] 定时签到开始 ================"
  } >> "$LOG_FILE"
fi

if [ -z "$PY" ]; then
  msg="找不到 Python 解释器。跑 install_vps.sh 装一下。"
  if [ "$DRYRUN" = "1" ]; then echo "❌ $msg"; exit 1; fi
  echo "[$(ts)] $msg" | tee -a "$LOG_FILE"
  echo "$msg" > "$HEARTBEAT"
  exit 1
fi

# ---------------------------------------------------------------- 锁
# flock 拿不到锁时必须**留痕**，否则 cron 触发了但脚本没跑，日志里什么都没有。
exec 9>"$LOCK_FILE"
if [ "${1:-}" != "--now" ]; then
  if ! flock -n 9; then
    if [ "$DRYRUN" = "1" ]; then
      echo "⚠️ 已有实例在跑，跳过（--now 可强制）"
      exit 0
    fi
    echo "[$(ts)] 已有实例在运行，本次跳过" >> "$LOG_FILE"
    exit 0
  fi
fi

# ---------------------------------------------------------------- 依赖自检
if [ "$DRYRUN" = "1" ]; then
  echo "== 解释器: $PY"
  "$PY" -c "import requests, cryptography; print('== 依赖 OK')" 2>&1 || true
  echo "== 配置:"
  if [ -f "$APP_DIR/.env" ]; then
    grep -E '^(PS_USER_PASS|PS_USER|LOGIN_AGENT_URL)=' "$APP_DIR/.env" \
      | sed -E 's/=(.{0,12}).*/=\1…/' || true
  else
    echo "  ⚠️ 没有 .env"
  fi
  echo "== agent 健康检查:"
  AGENT_URL=$(grep -E '^LOGIN_AGENT_URL=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"')
  [ -n "$AGENT_URL" ] && curl -s -m 5 "$AGENT_URL/health" | head -c 300 || echo "  (未配置或不可达)"
  echo
  echo "== Xvfb: $(command -v Xvfb || echo '未安装')"
  exit 0
fi

# ---------------------------------------------------------------- 执行
"$PY" "$APP_DIR/panstar_sign.py" 2>&1 | tee -a "$LOG_FILE"
CODE=${PIPESTATUS[0]}

{
  echo "[$(ts)] 结束，退出码 $CODE"
  echo "$(ts)" > "$HEARTBEAT"
} >> "$LOG_FILE"

exit $CODE
