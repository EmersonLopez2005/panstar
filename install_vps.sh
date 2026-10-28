#!/usr/bin/env bash
# PanstarCloud 自动签到 · VPS 一键安装（Debian 12/13、Ubuntu 22.04+）
#
#   bash install_vps.sh                      # 装依赖 + 浏览器(Camoufox) + venv
#   bash install_vps.sh --agent               # 额外注册 systemd 常驻服务（推荐）
#   bash install_vps.sh --engine=patchright   # 改用体积小的 Chromium 引擎（过 CF 成功率低）
#   bash install_vps.sh --no-cjk-fonts        # 跳过中文字体（省 ~250MB）
#   bash install_vps.sh --cache=/mnt/x        # 浏览器缓存放到别的盘（大盘机器救星）
#   bash install_vps.sh --force               # 跳过磁盘空间检查，硬装
#   bash install_vps.sh --check               # 只体检，不安装
#   bash install_vps.sh --cleanup-browsers    # 清理这台机器上多余的浏览器内核
#
# 设计说明：
#   - 引擎默认 Camoufox（Firefox 定制版）。Cloudflare 会把 Chromium 的
#     SwiftShader / headless 直接判成 bot，只有 Camoufox 的指纹伪装实测能过。
#   - 必须有头（headless=False）+ Xvfb，headless 一定过不了挑战。
#   - 磁盘占用：Camoufox 下载包 ~1.3GB + 解压后 ~1.3GB，**峰值约 2.6GB**；
#     换成 patchright 约 400MB。
#   - 本项目**不需要打码服务**，浏览器自己就能跑出 Turnstile token。

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
MODE="${1:-}"
ENGINE="camoufox"
CJK_FONTS=1
FORCE=0
CACHE_HOME=""

for arg in "$@"; do
  case "$arg" in
    --engine=*) ENGINE="${arg#*=}";;
    --cache=*)  CACHE_HOME="${arg#*=}";;
    --no-cjk-fonts) CJK_FONTS=0;;
    --force)    FORCE=1;;
  esac
done
case "$ENGINE" in
  camoufox|patchright|playwright) ;;
  *) echo "未知引擎：$ENGINE（可选 camoufox / patchright / playwright）" >&2; exit 1 ;;
esac

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 前置检查
if [ "$(id -u)" != "0" ] && ! command -v sudo >/dev/null 2>&1; then
  red "需要 root 或 sudo"
  exit 1
fi

SUDO=""
[ "$(id -u)" != "0" ] && SUDO="sudo"

if [ "$MODE" = "--check" ]; then
  step "仅体检模式"
  if [ -x "$PY" ]; then
    "$PY" "$APP_DIR/check_env.py" "${@:2}"
  else
    ylw "venv 还没建，只能查系统层面："
    python3 "$APP_DIR/check_env.py" --skip-browser
  fi
  exit 0
fi

if [ "$MODE" = "--cleanup-browsers" ]; then
  exec bash "$APP_DIR/cleanup_browsers.sh" "${@:2}"
fi

step "系统信息"
. /etc/os-release 2>/dev/null || true
echo "  ${PRETTY_NAME:-unknown}  $(uname -m)"
ARCH="$(uname -m)"
if [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "aarch64" ]; then
  ylw "非 x86_64/aarch64，浏览器可能没有现成构建"
fi

# ---------------------------------------------------------------- 系统依赖
step "安装系统依赖"
$SUDO apt-get update -qq
FONT_PKGS="fonts-liberation fonts-dejavu-core"
if [ "$CJK_FONTS" = "1" ]; then
  FONT_PKGS="fonts-noto-cjk $FONT_PKGS"
  echo "  安装中文字体（--no-cjk-fonts 可跳过，省约 250MB）"
else
  echo "  跳过中文字体（页面可能显示方框，但不影响签到接口）"
fi
$SUDO apt-get install -y --no-install-recommends \
  ca-certificates curl tzdata xvfb xauth \
  python3 python3-venv python3-pip \
  $FONT_PKGS \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 \
  libatspi2.0-0 libx11-xcb1 libxcb1 libxext6 libxi6 libxtst6 \
  libglib2.0-0 libdbus-1-3 libexpat1 libgtk-3-0 \
  libdbus-glib-1-2 libxt6 \
  libgl1-mesa-dri \
  >/dev/null
grn "系统依赖就绪"

# ---------------------------------------------------------------- 浏览器缓存位置
# 小硬盘机器的救命开关：把 1.3GB 的浏览器内核放到另一块盘上。
# 实现方式是「软链接 + XDG_CACHE_HOME」双保险 —— 不管 camoufox / playwright
# 内部是按 XDG_CACHE_HOME 还是按 ~/.cache 硬编码找路径，都能落到大盘上。
if [ -n "$CACHE_HOME" ]; then
  step "设置浏览器缓存位置"
  mkdir -p "$CACHE_HOME"
  mkdir -p "$HOME/.cache"
  for target in "$HOME/.cache/camoufox" "$HOME/.cache/ms-playwright"; do
    if [ -e "$target" ] && [ ! -L "$target" ]; then
      rm -rf "$target"
    fi
    rm -f "$target"
    ln -sfn "$CACHE_HOME/$(basename "$target")" "$target"
    echo "  $target -> $CACHE_HOME/$(basename "$target")"
  done
  # 写进 .env 的动作放到下面「配置文件」章节 —— .env 这时候才刚生成。
  # Camoufox 认 XDG_CACHE_HOME，Playwright/Patchright 认 PLAYWRIGHT_BROWSERS_PATH。
  grn "浏览器缓存已指向 $CACHE_HOME"
fi

# ---------------------------------------------------------------- 磁盘空间
# Camoufox 需要：下载包 ~1.3GB + 解压后 ~1.3GB，**同时存在 → 峰值约 2.6GB**。
# 只报"需要 1.3GB"会让人以为 1.5GB 够用，结果下到一半就 No space left。
case "$ENGINE" in
  camoufox)   NEED_MB=3072 ;;   # 2.6GB 峰值 + 一点余量
  patchright) NEED_MB=1024 ;;
  *)          NEED_MB=3072 ;;
esac
step "检查磁盘空间"
AVAIL_MB=$(df -Pm "$APP_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -z "$AVAIL_MB" ]; then
  ylw "  读不到磁盘信息，跳过检查"
else
  echo "  可用空间: ${AVAIL_MB} MB（$ENGINE 需要至少 ${NEED_MB} MB）"
  if [ "$AVAIL_MB" -lt "$NEED_MB" ] && [ "$FORCE" != "1" ]; then
    red "  ❌ 磁盘空间不足！"
    echo ""
    echo "  先定位大头并清理（会自动扫描全盘 Top 15）："
    echo "    bash $APP_DIR/cleanup_disk.sh --clean"
    echo ""
    echo "  如果这台机器有第二块盘，把浏览器内核挪过去（最有效）："
    echo "    bash $APP_DIR/install_vps.sh --engine=$ENGINE --cache=/mnt/你的大盘"
    echo ""
    echo "  空间实在不够就换轻量引擎（约 400MB，过 CF 成功率较低）："
    echo "    bash $APP_DIR/install_vps.sh --engine=patchright"
    echo ""
    echo "  也可以跳过中文字体省 ~250MB："
    echo "    bash $APP_DIR/install_vps.sh --no-cjk-fonts"
    echo ""
    echo "  实在不行就 --force 硬装（可能还是会在下载中途失败）："
    echo "    bash $APP_DIR/install_vps.sh --engine=$ENGINE --force"
    exit 1
  fi
  if [ "$AVAIL_MB" -lt "$NEED_MB" ]; then
    ylw "  ⚠️ 空间不足但已 --force，继续（下载中途可能失败）"
  else
    grn "磁盘空间充足"
  fi
fi

# ---------------------------------------------------------------- Python 环境
step "创建 venv"
if [ ! -x "$PY" ]; then
  $SUDO python3 -m venv "$VENV"
fi
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install -r "$APP_DIR/requirements.txt" --quiet
grn "Python 依赖就绪"

# ---------------------------------------------------------------- 浏览器
step "安装浏览器内核（$ENGINE）"
if [ "$ENGINE" = "camoufox" ]; then
  "$PY" -m pip install "camoufox[geoip]" --quiet
  "$PY" -m camoufox fetch
  grn "Camoufox 就绪（约 1.3GB，下载慢是正常的）"
else
  "$PY" -m pip install patchright --quiet
  "$PY" -m patchright install --with-deps chromium
  grn "Patchright Chromium 就绪（约 200MB）"
fi

# ---------------------------------------------------------------- 配置
step "配置文件"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  ylw "已生成 .env，请编辑后填账号："
  echo "    nano $APP_DIR/.env"
fi

# 浏览器缓存位置写进 .env，让 run_sign.sh / agent 每次都带上。
# Camoufox 认 XDG_CACHE_HOME，Playwright/Patchright 认 PLAYWRIGHT_BROWSERS_PATH。
if [ -n "$CACHE_HOME" ]; then
  set_env() {
    local key="$1" val="$2"
    if grep -qE "^${key}=" "$APP_DIR/.env"; then
      sed -i "s|^${key}=.*|${key}=${val}|" "$APP_DIR/.env"
    else
      printf '%s=%s\n' "$key" "$val" >> "$APP_DIR/.env"
    fi
  }
  set_env XDG_CACHE_HOME "$CACHE_HOME"
  set_env PLAYWRIGHT_BROWSERS_PATH "$CACHE_HOME/ms-playwright"
  grn "已把缓存路径写入 .env"
fi
mkdir -p "$APP_DIR/logs" "$APP_DIR/data" "$APP_DIR/cookie"

# ---------------------------------------------------------------- systemd
if [ "$MODE" = "--agent" ]; then
  step "注册 systemd 服务（浏览器代理）"
  AGENT_TOKEN_VALUE="$("$PY" - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  # 缓存路径：--cache= 指定的大盘，或默认 ~/.cache
  if [ -n "$CACHE_HOME" ]; then
    AGENT_CACHE_ENV="Environment=XDG_CACHE_HOME=$CACHE_HOME
Environment=PLAYWRIGHT_BROWSERS_PATH=$CACHE_HOME/ms-playwright"
  else
    AGENT_CACHE_ENV="Environment=XDG_CACHE_HOME=$HOME/.cache"
  fi
  cat > /etc/systemd/system/panstar-agent.service <<EOF
[Unit]
Description=PanstarCloud browser agent (Turnstile solver)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR/agent
Environment=AGENT_TOKEN=$AGENT_TOKEN_VALUE
Environment=AGENT_HOST=0.0.0.0
Environment=AGENT_PORT=3000
Environment=AGENT_HEADLESS=false
Environment=AGENT_ENGINE=$ENGINE
Environment=AGENT_PROFILE_DIR=$APP_DIR/agent/data/profiles
$AGENT_CACHE_ENV
Environment=TZ=Asia/Shanghai
ExecStart=$VENV/bin/python $APP_DIR/agent/panstar_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now panstar-agent
  sleep 3
  if $SUDO systemctl is-active --quiet panstar-agent; then
    grn "浏览器代理已启动：panstar-agent.service"
    echo "  健康检查: curl http://127.0.0.1:3000/health"
    echo "  AGENT_TOKEN=$AGENT_TOKEN_VALUE"
    echo "  → 把上面这行写进 $APP_DIR/.env 的 LOGIN_AGENT_TOKEN"
  else
    red "浏览器代理启动失败，看日志：journalctl -u panstar-agent -n 50"
  fi
fi

# ---------------------------------------------------------------- 定时任务
step "安装定时任务"
CRON_LINE="17 3 * * * $APP_DIR/run_sign.sh >> $APP_DIR/logs/cron.log 2>&1"
if crontab -l 2>/dev/null | grep -qF "$APP_DIR/run_sign.sh"; then
  ylw "定时任务已存在，跳过"
else
  ( crontab -l 2>/dev/null; echo "$CRON_LINE" ) | crontab -
  grn "已安装定时任务（每天 03:17）："
  echo "  $CRON_LINE"
fi

step "完成"
echo
echo "接下来："
echo "  1) 编辑账号配置： nano $APP_DIR/.env"
echo "  2) 先手动跑一次： bash $APP_DIR/run_sign.sh --now"
echo "  3) 看日志：       tail -f $APP_DIR/logs/sign.log"
