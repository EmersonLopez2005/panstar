#!/usr/bin/env bash
# 磁盘清理 —— Camoufox 需要约 1.3GB，VPS 空间不够时先跑这个。
#
# 用法：
#   bash cleanup_disk.sh          # 只报告，不删
#   bash cleanup_disk.sh --clean  # 真删
#
# 删的都是「可再生」的东西：包缓存、旧的浏览器内核、日志。
# 不会碰：项目目录、.env、浏览器 profile（里面是 Cloudflare 的设备指纹，删了要重新过挑战）。
set -uo pipefail

MODE="${1:-}"
CLEAN=0
[ "$MODE" = "--clean" ] && CLEAN=1

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
hr()  { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

df_h() { df -Ph "$1" 2>/dev/null | awk 'NR==2 {print $4}'; }
sz_of() { [ -e "$1" ] && du -sh "$1" 2>/dev/null | cut -f1 || echo "-"; }

BEFORE=$(df -Ph "$APP_DIR" | awk 'NR==2 {print $4}')

hr "当前磁盘"
df -Ph "$APP_DIR" | awk 'NR==1 || NR==2'
echo ""

total_freed=0

# ---------------------------------------------------------------- apt 缓存
hr "apt 包缓存"
APT_CACHE="/var/cache/apt/archives"
SZ=$(sz_of "$APT_CACHE")
echo "  $APT_CACHE  ($SZ)"
if [ "$CLEAN" = "1" ]; then
  sudo apt-get clean 2>/dev/null
  sudo apt-get autoremove -y --purge >/dev/null 2>&1
  echo "  已清理"
else
  echo "  加 --clean 才真删"
fi

# ---------------------------------------------------------------- 浏览器内核
hr "浏览器内核（Camoufox 约 1.3GB）"
for d in "$HOME/.cache/camoufox" "$HOME/.cache/ms-playwright" "$HOME/.cache/patchright"; do
  if [ -d "$d" ]; then
    echo "  $d  ($(sz_of "$d"))"
    if [ "$CLEAN" = "1" ]; then
      rm -rf "$d"
      echo "    已删除"
    fi
  fi
done
[ "$CLEAN" = "1" ] && echo "  重新安装：python -m camoufox fetch"

# ---------------------------------------------------------------- Docker
hr "Docker"
if command -v docker >/dev/null 2>&1; then
  USED=$(docker system df 2>/dev/null | grep -i "images" | head -1)
  echo "  ${USED:-（无输出）}"
  if [ "$CLEAN" = "1" ]; then
    docker system prune -f >/dev/null 2>&1
    docker builder prune -f >/dev/null 2>&1
    echo "  已清理悬空镜像与构建缓存"
  else
    echo "  加 --clean 才真删"
  fi
else
  echo "  (没装 docker)"
fi

# ---------------------------------------------------------------- 日志
hr "系统日志"
JOURNAL=$(journalctl --disk-usage 2>/dev/null | grep -oE "[0-9.]+[KMG]" | head -1)
echo "  journald: ${JOURNAL:-未知}"
if [ "$CLEAN" = "1" ]; then
  sudo journalctl --vacuum-size=100M >/dev/null 2>&1
  echo "  已压缩到 100M"
fi

# ---------------------------------------------------------------- 项目残留
hr "项目内的临时文件"
for d in "$APP_DIR/data/check-profile" "$APP_DIR/data/browser" "$APP_DIR/agent/data/profiles"; do
  if [ -d "$d" ]; then
    echo "  $d  ($(sz_of "$d"))"
  fi
done
echo "  ⚠️ profile 里有 Cloudflare 设备指纹，删了要重新过挑战 —— 保留"

# ---------------------------------------------------------------- 旧日志
hr "项目日志"
LOGS="$APP_DIR/logs"
if [ -d "$LOGS" ]; then
  echo "  $LOGS  ($(sz_of "$LOGS"))"
  if [ "$CLEAN" = "1" ]; then
    find "$LOGS" -name "*.log" -mtime +7 -delete 2>/dev/null
    echo "  已删 7 天前的日志"
  fi
fi

# ---------------------------------------------------------------- 汇总
hr "汇总"
AFTER=$(df -Ph "$APP_DIR" | awk 'NR==2 {print $4}')
echo "  清理前可用: $BEFORE"
echo "  清理后可用: $AFTER"
echo ""
if [ "$CLEAN" = "1" ]; then
  if [ "$BEFORE" != "$AFTER" ]; then
    grn "✅ 已释放空间"
  else
    ylw "空间没变化 —— 可能大头不在这些目录里，用下面这条找找："
    echo "  sudo du -xh / --max-depth=1 2>/dev/null | sort -h | tail -15"
  fi
  echo ""
  echo "  下一步："
  echo "    bash install_vps.sh --engine=camoufox   # 重新装浏览器"
  echo "    python3 check_env.py                     # 体检（会先查磁盘）"
else
  ylw "这是预演，加 --clean 才真删"
fi
