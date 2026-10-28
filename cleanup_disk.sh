#!/usr/bin/env bash
# 磁盘清理 + 空间体检 —— Camoufox 需要约 1.3GB，VPS 空间不够时先跑这个。
#
# 用法：
#   bash cleanup_disk.sh            # 只报告，不删
#   bash cleanup_disk.sh --clean    # 真删（安全项目：只删可再生内容）
#   bash cleanup_disk.sh --aggressive  # 真删 + 激进清理（会删未使用的 Docker 镜像等）
#
# 删的都是「可再生」的东西：包缓存、旧的浏览器内核、日志、编译缓存。
# 不会碰：项目目录、.env、浏览器 profile（里面是 Cloudflare 的设备指纹，删了要重新过挑战）。
#
# 关键设计：
#   1) 前后对比用 **MB 精度**（df -Pm）。用 df -Ph 取整到 G，
#      释放几百 MB 也会显示成"没变化"，白白误导人。
#   2) 自动扫描全盘 Top 大户（du -xh --max-depth=1 /），不用手动找。
#      清理完还是不够时，直接告诉你空间到底在哪。
set -uo pipefail

MODE="${1:-}"
CLEAN=0
AGGRESSIVE=0
case "$MODE" in
  --clean) CLEAN=1 ;;
  --aggressive) CLEAN=1; AGGRESSIVE=1 ;;
esac

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
hr()  { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

# 需要 root 才能读的部分
SUDO=""
[ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

sz_of() { [ -e "$1" ] && du -sh "$1" 2>/dev/null | cut -f1 || echo "-"; }
# 返回 KB 整数
kb_of() { [ -e "$1" ] && du -sk "$1" 2>/dev/null | cut -f1 || echo 0; }
# 可用空间（MB，整数）
avail_mb() { df -Pm "$APP_DIR" 2>/dev/null | awk 'NR==2 {print $4}'; }

BEFORE_MB=$(avail_mb)

hr "当前磁盘"
df -Ph "$APP_DIR" | awk 'NR==1 || NR==2'
echo "  可用: ${BEFORE_MB} MB"
echo ""

# ---------------------------------------------------------------- 全盘体检
# 放在清理之前跑，这样即使什么都没删，也能立刻知道空间在哪。
hr "全盘空间分布（Top 15）"
echo "  （这一段可能要跑几十秒，磁盘很大时尤其慢）"
TOP_OUT=$(timeout 90 $SUDO du -xh --max-depth=1 / 2>/dev/null | sort -k1 -h | tail -15)
if [ -n "$TOP_OUT" ]; then
  echo "$TOP_OUT" | awk '{printf "    %-8s %s\n", $1, $2}'
else
  ylw "  扫描超时或权限不足，跳过"
fi

hr "细项（/var /root /home /tmp）"
for d in /var /root /home /tmp /usr; do
  [ -d "$d" ] || continue
  printf '    %-8s %s\n' "$(du -sh "$d" 2>/dev/null | cut -f1)" "$d"
done
echo ""
echo "  最大的几个文件（>100MB）："
timeout 60 $SUDO find / -xdev -type f -size +100M 2>/dev/null \
  -printf '    %10s  %p\n' 2>/dev/null | sort -k1 -h | tail -12 || true

# ---------------------------------------------------------------- apt 缓存
hr "apt 包缓存"
APT_CACHE="/var/cache/apt/archives"
echo "  $APT_CACHE  ($(sz_of "$APT_CACHE"))"
if [ "$CLEAN" = "1" ]; then
  $SUDO apt-get clean 2>/dev/null
  $SUDO apt-get autoremove -y --purge >/dev/null 2>&1
  echo "  已清理 apt 缓存 + 无用依赖"
else
  echo "  加 --clean 才真删"
fi

# apt lists 单独处理（常常有 100MB+，autoremove 不会动它）
APT_LISTS="/var/lib/apt/lists"
if [ -d "$APT_LISTS" ]; then
  echo "  $APT_LISTS  ($(sz_of "$APT_LISTS"))"
  if [ "$CLEAN" = "1" ]; then
    $SUDO rm -rf "$APT_LISTS"/* 2>/dev/null
    echo "    已删除索引（apt-get update 会重建）"
  fi
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
  $SUDO docker system df 2>/dev/null | sed 's/^/    /' || echo "  (docker 守护进程没起来)"
  if [ "$CLEAN" = "1" ]; then
    $SUDO docker container prune -f >/dev/null 2>&1
    $SUDO docker image prune -f >/dev/null 2>&1
    $SUDO docker builder prune -f >/dev/null 2>&1
    echo "  已清理：停止的容器 / 悬空镜像 / 构建缓存"
    if [ "$AGGRESSIVE" = "1" ]; then
      $SUDO docker image prune -a -f >/dev/null 2>&1
      $SUDO docker network prune -f >/dev/null 2>&1
      echo "  已额外清理：未被任何容器使用的镜像（--aggressive）"
      ylw "  ⚠️ 被删的镜像下次要用得重新 pull"
    fi
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
  $SUDO journalctl --vacuum-size=50M >/dev/null 2>&1
  echo "  已压缩到 50M"
fi
# 大体积的普通日志文件
BIGLOGS=$($SUDO find /var/log -xdev -type f -size +20M 2>/dev/null | head -10)
if [ -n "$BIGLOGS" ]; then
  echo "  /var/log 下的大文件："
  echo "$BIGLOGS" | while read -r f; do echo "    $(sz_of "$f")  $f"; done
  if [ "$CLEAN" = "1" ]; then
    echo "$BIGLOGS" | while read -r f; do
      case "$f" in
        */journal/*) : ;;               # journal 由 vacuum 处理
        *.gz) $SUDO rm -f "$f" 2>/dev/null && echo "      已删 $f" ;;
        *) $SUDO truncate -s 0 "$f" 2>/dev/null && echo "      已清空 $f" ;;
      esac
    done
  fi
fi

# ---------------------------------------------------------------- 其它缓存
hr "其它缓存"
for d in "$HOME/.cache/pip" "$HOME/.npm" "$HOME/.cargo" "/var/tmp" "$HOME/.cache/uv"; do
  [ -e "$d" ] || continue
  echo "  $d  ($(sz_of "$d"))"
  if [ "$CLEAN" = "1" ]; then
    rm -rf "${d:?}"/* 2>/dev/null
    echo "    已清空"
  fi
done
# 老旧 snap
if command -v snap >/dev/null 2>&1 && [ "$CLEAN" = "1" ]; then
  snap list --all 2>/dev/null | awk '/disabled/{print $1}' | while read -r s; do
    $SUDO snap remove "$s" >/dev/null 2>&1 && echo "  已删旧 snap: $s"
  done
fi
# /tmp 里的老文件
if [ "$CLEAN" = "1" ]; then
  $SUDO find /tmp -xdev -maxdepth 1 -mtime +7 -type f -delete 2>/dev/null
  echo "  已删 /tmp 下 7 天前的文件"
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
AFTER_MB=$(avail_mb)
FREED=$(( AFTER_MB - BEFORE_MB ))
echo "  清理前可用: ${BEFORE_MB} MB"
echo "  清理后可用: ${AFTER_MB} MB"
if [ "$FREED" -gt 0 ]; then
  grn "  ✅ 释放了 ${FREED} MB"
elif [ "$FREED" -eq 0 ]; then
  ylw "  ➖ 一点都没释放 —— 说明这些目录本来就是干净的"
else
  red "  ❌ 可用空间反而少了 $(( -FREED )) MB（有别的进程在写盘）"
fi
echo ""

if [ "$CLEAN" != "1" ]; then
  ylw "这是预演，加 --clean 才真删"
  exit 0
fi

# ---------------------------------------------------------------- 还能怎么办
echo "  Camoufox 装完至少要 2.6GB（下载包 ~1.3GB + 解压后 ~1.3GB 同时存在）。"
echo "  如果现在可用空间还是不够，按下面顺序处理："
echo ""
echo "  1) 看上面「全盘空间分布」的 Top 15 —— 找到真正的大头"
echo "     （常见元凶：/var/lib/docker 里的旧镜像、/root 下别的项目、/var/log、snap）"
echo ""
echo "  2) 换一个大盘挂载点给浏览器缓存（最有效，不动其它数据）："
echo "     export PS_CACHE_HOME=/mnt/大硬盘/camoufox-cache"
echo "     mkdir -p \"\$PS_CACHE_HOME\""
echo "     bash install_vps.sh --engine=camoufox   # 会自动把缓存指过去"
echo ""
echo "  3) 换轻量引擎（约 400MB，过 CF 成功率较低）："
echo "     bash install_vps.sh --engine=patchright"
echo ""
echo "  4) 装中文字体很占空间（~250MB），空间紧张时跳过："
echo "     bash install_vps.sh --no-cjk-fonts"
echo ""
echo "  5) 还是不够就扩盘，或换台空间大的机器。"
