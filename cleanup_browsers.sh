#!/usr/bin/env bash
# 清理这台机器上多余的浏览器内核。
#
# 为什么需要：
#   - Camoufox 约 1.3GB，patchright 的 Chromium 约 200MB；
#   - 切换引擎（--engine=）时旧内核不会被自动删，反复切换能吃掉好几 GB；
#   - playwright 缓存目录 ~/.cache/ms-playwright 常常有多个版本残留。
#
# 用法：
#   bash cleanup_browsers.sh            # 只报告，不删
#   bash cleanup_browsers.sh --dryrun   # 同上
#   bash cleanup_browsers.sh --clean    # 真删

set -uo pipefail

MODE="${1:-}"
CLEAN=0
[ "$MODE" = "--clean" ] && CLEAN=1

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

total_before=0
total_after=0

hr() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- camoufox
hr "Camoufox 缓存"
CAMOUFOX_DIR="$HOME/.cache/camoufox"
if [ -d "$CAMOUFOX_DIR" ]; then
  sz=$(du -sh "$CAMOUFOX_DIR" 2>/dev/null | cut -f1)
  total_before=$((total_before + $(du -sk "$CAMOUFOX_DIR" 2>/dev/null | cut -f1)))
  echo "  $CAMOUFOX_DIR  ($sz)"
  if [ "$CLEAN" = "1" ]; then
    rm -rf "$CAMOUFOX_DIR"
    echo "  已删除"
  else
    echo "  加 --clean 才真删"
  fi
else
  echo "  (没有)"
fi

# ---------------------------------------------------------------- playwright
hr "Playwright / Patchright 缓存"
PW_DIR="$HOME/.cache/ms-playwright"
if [ -d "$PW_DIR" ]; then
  for d in "$PW_DIR"/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    sz=$(du -sh "$d" 2>/dev/null | cut -f1)
    echo "  $name  ($sz)"
    total_before=$((total_before + $(du -sk "$d" 2>/dev/null | cut -f1)))
    if [ "$CLEAN" = "1" ]; then
      rm -rf "$d"
      echo "    已删除"
    fi
  done
else
  echo "  (没有)"
fi

# ---------------------------------------------------------------- 旧 profile
hr "浏览器 profile"
PROFILE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for p in "$PROFILE_ROOT/data/browser" "$PROFILE_ROOT/agent/data/profiles"; do
  if [ -d "$p" ]; then
    sz=$(du -sh "$p" 2>/dev/null | cut -f1)
    echo "  $p  ($sz)"
    total_before=$((total_before + $(du -sk "$p" 2>/dev/null | cut -f1)))
    echo "  ⚠️ 删除会丢失指纹（Cloudflare 会把你当新设备），建议保留"
  fi
done

# ---------------------------------------------------------------- 汇总
hr "汇总"
after=$(( total_after ))
if [ "$CLEAN" = "1" ]; then
  grn "清理完成，重新跑 install_vps.sh 可恢复需要的内核"
else
  ylw "这是预演，加 --clean 才真删"
  echo "  可释放约 $(( total_before / 1024 )) MB"
fi
