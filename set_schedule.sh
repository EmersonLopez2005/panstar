#!/usr/bin/env bash
# PanstarCloud 自动签到 · 修改定时任务时间
#
#   bash set_schedule.sh                 # 查看当前定时任务
#   bash set_schedule.sh 07:30           # 改成每天 07:30
#   bash set_schedule.sh 07:30,20:00     # 每天 07:30 和 20:00 各跑一次
#   bash set_schedule.sh --remove        # 删除定时任务
#   bash set_schedule.sh --cron "0 */6 * * *"   # 直接用 cron 表达式
#
# 设计说明：
#   - 只动本项目的行，**不碰你其它 crontab 条目**
#   - 幂等：重复执行不会堆出多条
#   - 时间用服务器本地时区（显示在输出里，注意核对）
#   - cron 没装会自动装

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_SH="$APP_DIR/run_sign.sh"
LOG="$APP_DIR/logs/cron.log"
MARK="# panstar-autosign"   # 用来标记本项目的行，便于识别与清理

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

SUDO=""
[ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

# ---------------------------------------------------------------- 环境
ensure_cron() {
  if command -v crontab >/dev/null 2>&1; then
    return 0
  fi
  ylw "系统没装 cron，正在安装"
  $SUDO apt-get install -y --no-install-recommends cron >/dev/null 2>&1 || true
  if ! command -v crontab >/dev/null 2>&1; then
    red "❌ cron 装不上，无法设置定时任务"
    echo "   手动： apt-get install cron"
    return 1
  fi
  $SUDO systemctl enable cron >/dev/null 2>&1 || true
  $SUDO systemctl restart cron >/dev/null 2>&1 || true
  grn "cron 已安装"
}

# 读现有 crontab。
#
# **必须单独读、读成功再动手，绝不能在管道里"边读边写"。**
# 踩过的坑：`{ 读; echo 新行; } | crontab -` 这种写法，
# 读和写会并发，读可能拿到空内容 → 把用户所有其它定时任务全冲掉
# （备份、证书续期、监控……）。这是灾难级错误，必须防住。
CURRENT_CRON=""
read_current_cron() {
  local out rc err
  out="$(crontab -l 2>/dev/null)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    CURRENT_CRON="$out"
    return 0
  fi
  # 区分「本来就没有任务」和「读失败」
  err="$(crontab -l 2>&1 >/dev/null || true)"
  case "$err" in
    *"no crontab"*|*"No crontab"*)
      CURRENT_CRON=""
      return 0
      ;;
  esac
  red "❌ 读不到现有 crontab，为避免覆盖你的任务，已中止"
  echo "   错误信息: ${err:-（无输出，退出码 $rc）}"
  echo "   手动查看: crontab -l"
  return 1
}

# 只滤掉本项目的行，其它条目原样保留（用变量里的内容，不再调用 crontab）
other_cron() {
  printf '%s\n' "$CURRENT_CRON" \
    | grep -vF "$RUN_SH" \
    | grep -vF "$MARK" \
    | grep -v '^[[:space:]]*$' || true
}

# 提示可能残留的旧条目（路径变过、或手工加过没带标记）
warn_leftover() {
  local left
  left="$(printf '%s\n' "$CURRENT_CRON" \
          | grep -F 'run_sign.sh' \
          | grep -vF "$RUN_SH" \
          | grep -vF "$MARK" || true)"
  if [ -n "$left" ]; then
    ylw "  注意：crontab 里还有其它引用 run_sign.sh 的条目（路径不同），没有动它："
    printf '%s\n' "$left" | sed 's/^/    /'
    echo "    如果确认是旧项目残留，手动删： crontab -e"
  fi
}

# 写之前先备份 —— 改 crontab 一旦出错，影响的是用户所有定时任务
BACKUP_PATH=""
backup_crontab() {
  BACKUP_PATH=""
  [ -z "$CURRENT_CRON" ] && return 0
  local dir="$APP_DIR/logs"
  mkdir -p "$dir" 2>/dev/null || return 0
  local f="$dir/crontab.backup.$(date +%Y%m%d-%H%M%S).txt"
  if printf '%s\n' "$CURRENT_CRON" > "$f" 2>/dev/null; then
    BACKUP_PATH="$f"
  fi
  return 0
}

show_current() {
  step "当前定时任务"
  echo "  服务器时间: $(date '+%Y-%m-%d %H:%M:%S  %Z (%z)')"
  local mine
  # 重新读一次，保证显示的是刚落盘的内容
  CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
  mine="$(printf '%s\n' "$CURRENT_CRON" | grep -F "$RUN_SH" || true)"
  if [ -z "$mine" ]; then
    ylw "  还没有配置定时任务"
    echo "  设置： bash $APP_DIR/set_schedule.sh 07:30"
    return 0
  fi
  echo ""
  echo "$mine" | sed 's/^/  /'
  echo ""
  # 尽量算出下次运行时间（只对 HH:MM 形式有效）
  local spec
  spec="$(echo "$mine" | head -1 | awk '{print $1" "$2}')"
  local hh mm
  hh="$(echo "$spec" | awk '{print $2}')"
  mm="$(echo "$spec" | awk '{print $1}')"
  if [[ "$hh" =~ ^[0-9]+$ ]] && [[ "$mm" =~ ^[0-9]+$ ]]; then
    local now target
    now="$(date +%s)"
    target="$(date -d "today $(printf '%02d:%02d' "$hh" "$mm")" +%s 2>/dev/null || echo 0)"
    if [ "$target" -le "$now" ]; then
      target="$(date -d "tomorrow $(printf '%02d:%02d' "$hh" "$mm")" +%s 2>/dev/null || echo 0)"
    fi
    [ "$target" -gt 0 ] && echo "  下次运行: $(date -d "@$target" '+%Y-%m-%d %H:%M')"
  fi
  echo ""
  echo "  查看日志: tail -f $LOG"
}

# ---------------------------------------------------------------- 参数
MODE="show"
SPEC=""
CRON_EXPR=""

for arg in "$@"; do
  case "$arg" in
    --remove|--delete|--clear) MODE="remove" ;;
    --show|-l|--list)          MODE="show" ;;
    --cron)                    MODE="expr" ;;
    --cron=*)                  MODE="expr"; CRON_EXPR="${arg#*=}" ;;
    --help|-h)                 MODE="help" ;;
    *)                         SPEC="$arg" ;;
  esac
done

# --cron 后跟表达式
if [ "$MODE" = "expr" ] && [ -z "$CRON_EXPR" ]; then
  for arg in "$@"; do
    case "$arg" in
      --cron) ;;
      --*) ;;
      *) CRON_EXPR="$arg" ;;
    esac
  done
fi

if [ "$MODE" = "remove" ]; then
  ensure_cron || exit 1
  step "删除定时任务"
  read_current_cron || exit 1
  backup_crontab || exit 1
  other_cron | crontab -
  grn "已删除本项目的定时任务（其它 crontab 条目未动）"
  show_current
  exit 0
fi

if [ "$MODE" = "help" ]; then
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

if [ -z "$SPEC" ] && [ "$MODE" != "expr" ]; then
  show_current
  echo ""
  echo "改时间： bash $APP_DIR/set_schedule.sh 07:30"
  echo "多个时间： bash $APP_DIR/set_schedule.sh 07:30,20:00"
  exit 0
fi

# ---------------------------------------------------------------- 校验参数
ensure_cron || exit 1

# **校验必须在主流程里做，不能放进 $(...) 里。**
# 踩过的坑：build_lines 里写 exit 1，但它是 `NEW_LINES="$(build_lines)"`
# 的命令替换子 shell —— 那里的 exit 只退出子 shell，主脚本继续往下跑，
# 结果输入非法时仍打印「已更新」并返回 0（假成功），
# 后面拿空串做算术还会抛 "[: 0: integer expected"。
if [ "$MODE" = "expr" ]; then
  if [ -z "$CRON_EXPR" ]; then
    red "❌ --cron 后面要跟表达式，例如： --cron \"0 */6 * * *\""
    exit 1
  fi
  fields="$(printf '%s\n' "$CRON_EXPR" | awk '{print NF}')"
  if [ "$fields" -ne 5 ]; then
    red "❌ cron 表达式应该是 5 段（分 时 日 月 周），你给了 $fields 段"
    echo "   例子： 0 7 * * *   → 每天 07:00"
    exit 1
  fi
else
  for t in $(printf '%s' "$SPEC" | tr ',' ' '); do
    if ! [[ "$t" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]]; then
      red "❌ 时间格式不对：$t"
      echo "   应该是 HH:MM（24 小时制），例如 07:30、20:05"
      exit 1
    fi
  done
fi

# ---------------------------------------------------------------- 构造 cron
build_lines() {
  if [ "$MODE" = "expr" ]; then
    printf '%s %s >> %s 2>&1 %s\n' "$CRON_EXPR" "$RUN_SH" "$LOG" "$MARK"
    return 0
  fi
  local IFS=','
  for t in $SPEC; do
    local h m
    h=$((10#${t%%:*}))
    m=$((10#${t##*:}))
    printf '%d %d * * * %s >> %s 2>&1 %s\n' "$m" "$h" "$RUN_SH" "$LOG" "$MARK"
  done
}

NEW_LINES="$(build_lines)"
if [ -z "$NEW_LINES" ]; then
  red "❌ 没有生成任何定时任务（参数解析异常）"
  exit 1
fi

step "更新定时任务"
mkdir -p "$APP_DIR/logs"
chmod +x "$RUN_SH" 2>/dev/null || true

read_current_cron || exit 1
backup_crontab || exit 1

# 先在变量里拼好完整内容，再一次性写入 —— 避免边读边写
FINAL_CRON="$(other_cron)"
if [ -n "$FINAL_CRON" ]; then
  FINAL_CRON="$FINAL_CRON
$NEW_LINES"
else
  FINAL_CRON="$NEW_LINES"
fi

# ---------------------------------------------------------------- 安全校验
# 只在能解析成纯数字时才比较，避免空串/多行导致 "integer expected"
count_lines() {
  local s="${1:-}"
  if [ -z "$s" ]; then echo 0; return; fi
  local n
  n="$(printf '%s\n' "$s" | grep -c '[^[:space:]]' 2>/dev/null)"
  case "$n" in
    ''|*[!0-9]*) echo 0 ;;
    *) echo "$n" ;;
  esac
}
before_n=$(count_lines "$CURRENT_CRON")
other_n=$(count_lines "$FINAL_CRON")
mine_n=$(count_lines "$(printf '%s\n' "$CURRENT_CRON" | grep -F "$RUN_SH" || true)")

if [ "$before_n" -gt "$mine_n" ] && [ "$other_n" -le "$(count_lines "$NEW_LINES")" ]; then
  red "❌ 安全检查未通过：原 crontab 有 $before_n 行（其中 $mine_n 行属于本项目）"
  red "   但过滤后「其它条目」一行都没剩 —— 疑似误删"
  echo "   为防止破坏你的定时任务，已中止（原 crontab 未修改）"
  echo "   备份位置: ${BACKUP_PATH:-（未备份）}"
  exit 1
fi

printf '%s\n' "$FINAL_CRON" | crontab -

grn "已更新（其它 crontab 条目未动）"
[ -n "${BACKUP_PATH:-}" ] && echo "  原 crontab 已备份: $BACKUP_PATH"
warn_leftover
echo ""
echo "$NEW_LINES" | sed 's/^/  /'
echo ""
show_current
