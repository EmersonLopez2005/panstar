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

# ---------------------------------------------------------------- 坏软链接修复
# **判断依据：是个软链接，但 `-d`（is_dir）为假 → 自环或断链 → 删掉。**
#
# 为什么必须修：自环软链接（~/.cache/camoufox -> ~/.cache/camoufox）会让
# camoufox 的 INSTALL_DIR.mkdir(parents=True, exist_ok=True) 抛
#   FileExistsError: [Errno 17] File exists: '/root/.cache/camoufox'
# 因为 pathlib 判断「已存在且是目录」用的是 is_dir()，而自环的 is_dir()
# 会因 ELOOP 返回 False，于是 exist_ok=True 也救不了。
# 一次坏链接能让之后每一次 camoufox fetch 都失败 —— 必须无条件清理。
repair_broken_links() {
  local target="$1"
  [ -L "$target" ] || return 0
  if [ ! -d "$target" ]; then
    rm -f "$target"
    ylw "  已移除损坏的软链接：$target"
    return 0
  fi
  # 是软链接且指向有效目录 → 再查是不是指回自己
  local raw dest self
  raw="$(readlink "$target" 2>/dev/null || true)"
  case "$raw" in
    /*) dest="$raw" ;;
    *)  dest="$(dirname "$target")/$raw" ;;
  esac
  self="$(cd "$(dirname "$target")" && pwd)/$(basename "$target")"
  if [ "$dest" = "$self" ]; then
    rm -f "$target"
    ylw "  已移除自环软链接：$target"
  fi
}

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

# ---------------------------------------------------------------- 机器规格
# Camoufox（Firefox 定制版）单个实例常驻 300-500MB。
# 1 核 / 1GB 的机器跑浏览器签到非常勉强 —— 两个浏览器实例同时起来就会
# swap 抖动，表现为 CPU 100%、机器卡死、日志一行都出不来。
LOW_MEMORY=0
MEM_TOTAL_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
CPU_CORES=$(nproc 2>/dev/null || echo 1)
echo "  内存 ${MEM_TOTAL_MB} MB / CPU ${CPU_CORES} 核"
if [ "${MEM_TOTAL_MB:-0}" -lt 1200 ] 2>/dev/null; then
  LOW_MEMORY=1
  ylw "  ⚠️ 小内存机器（${MEM_TOTAL_MB} MB）：浏览器签到会很吃力"
  echo "     已自动收紧资源限制（agent 限 600M、小内存模式关掉 humanize）。"
  echo "     若仍卡死，建议换一台内存 ≥ 2GB 的 VPS，或加 swap："
  echo "       fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile"
fi
if [ "$LOW_MEMORY" = "1" ]; then
  export PS_LOW_MEMORY=1
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
  # 规范化成绝对路径再做比较 —— 直接比字符串会因为斜杠写法不同而漏判，
  # 结果又造出自环软链接。
  CACHE_HOME="$(cd "$CACHE_HOME" && pwd)"
  # **非 ASCII 路径要提醒。**
  # Firefox 的 native 代码路径处理、zip 解压、systemd 单元文件
  # 都可能对中文/非 ASCII 路径出意外（乱码、找不到文件）。
  # 能用就继续，但必须明确告知。
  if ! printf '%s' "$CACHE_HOME" | LC_ALL=C grep -q '^[[:print:]]*$'; then
    ylw "  ⚠️ 缓存路径含非 ASCII 字符：$CACHE_HOME"
    echo "     Firefox / systemd 对中文路径可能出意外。"
    echo "     建议改用纯英文路径，例如："
    echo "       bash $APP_DIR/install_vps.sh --cache=/mnt/browser --no-cjk-fonts"
  fi
  for target in "$HOME/.cache/camoufox" "$HOME/.cache/ms-playwright"; do
    name="$(basename "$target")"
    dest="$CACHE_HOME/$name"
    # **自环必须跳过。**
    # 当 --cache 指向的就是 $HOME/.cache 本身时（如 --cache=/root/.cache），
    # 「$HOME/.cache/camoufox -> /root/.cache/camoufox」是指向自己的软链接。
    # 这种自环会让 mkdir(exist_ok=True) 抛 FileExistsError
    # （is_dir() 因 ELOOP 返回 False），camoufox 直接崩掉。
    if [ "$(cd "$(dirname "$target")" && pwd)/$name" = "$dest" ]; then
      # 目标就是它自己 —— 保证它是个真实目录（顺手清掉可能存在的旧自环）
      if [ -L "$target" ]; then
        rm -f "$target"
      fi
      mkdir -p "$target"
      echo "  $target（目标位置与自身相同，直接使用真实目录）"
      continue
    fi
    if [ -e "$target" ] && [ ! -L "$target" ]; then
      rm -rf "$target"
    fi
    rm -f "$target"
    ln -sfn "$dest" "$target"
    echo "  $target -> $dest"
  done
  # 建完再统一体检一次，确保没有自环残留
  for target in "$HOME/.cache/camoufox" "$HOME/.cache/ms-playwright"; do
    repair_broken_links "$target"
  done
  # **必须在这里 export，不能只写进 .env。**
  # camoufox 的 INSTALL_DIR = Path(user_cache_dir("camoufox"))，
  # user_cache_dir 读的是 XDG_CACHE_HOME —— 写进 .env 要等下次进程启动才生效，
  # 而「下载浏览器」就发生在当前进程里。不 export 的话，
  # camoufox 会把内核装到 ~/.cache/camoufox，跟你指定的目录不是一回事。
  export XDG_CACHE_HOME="$CACHE_HOME"
  export PLAYWRIGHT_BROWSERS_PATH="$CACHE_HOME/ms-playwright"
  # 写进 .env 的动作放到下面「配置文件」章节 —— .env 这时候才刚生成。
  grn "浏览器缓存已指向 $CACHE_HOME"
  grn "已 export XDG_CACHE_HOME / PLAYWRIGHT_BROWSERS_PATH（本进程立即生效）"
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
# agent 依赖也必须装 —— 只装主 requirements.txt 会缺 flask，
# 结果 systemd 里的 panstar-agent 起来就崩（ModuleNotFoundError: flask）。
"$PY" -m pip install -r "$APP_DIR/agent/requirements.txt" --quiet
grn "Python 依赖就绪（含 agent 依赖）"

# ---------------------------------------------------------------- 浏览器
step "安装浏览器内核（$ENGINE）"

# ---------------------------------------------------------------- 清理坏软链接
# **这一步必须无条件执行，不能只在传了 --cache 时才做。**
#
# 历史 bug：早期版本在 --cache 指向 $HOME/.cache 时造出过自环软链接
# （~/.cache/camoufox -> ~/.cache/camoufox）。那种链接一旦留在机器上，
# 之后每次 camoufox fetch 都会在 INSTALL_DIR.mkdir(exist_ok=True) 处炸：
#   FileExistsError: [Errno 17] File exists: '/root/.cache/camoufox'
# 因为自环软链接的 is_dir() 因 ELOOP 返回 False，pathlib 判定「不是目录」，
# 于是重新抛出 FileExistsError。删掉坏链接即可恢复。
for t in "$HOME/.cache/camoufox" "$HOME/.cache/ms-playwright"; do
  repair_broken_links "$t"
done

# ---------------------------------------------------------------- TMPDIR
# **TMPDIR 必须是关键变量。**
# camoufox 内部用 `tempfile.NamedTemporaryFile()`（不指定 dir）来存下载的 zip，
# 也就是落在 `$TMPDIR` 或 `/tmp`。
# 很多 VPS 把 /tmp 挂成 tmpfs（内存盘，通常只有几百 MB），
# 于是会出现「df 显示还有 6GB，但下载到 0.5GB 就 No space left on device」。
# 把 TMPDIR 指到大盘上，这个坑就绕过去了。
#
# 优先级：
#   1. 用户已经 export 的 TMPDIR（手动指定过，尊重它，但会校验空间）
#   2. --cache 指定的大盘下的 tmp/ 子目录
#   3. /tmp
if [ -n "${TMPDIR:-}" ] && [ "$TMPDIR" != "/tmp" ]; then
  TMP_BASE="$TMPDIR"
  echo "  使用你已设置的 TMPDIR"
elif [ -n "$CACHE_HOME" ]; then
  TMP_BASE="$CACHE_HOME/tmp"
else
  TMP_BASE="/tmp"
fi
export TMPDIR="$TMP_BASE"
mkdir -p "$TMPDIR" 2>/dev/null || true
TMP_AVAIL_MB=$(df -Pm "$TMPDIR" 2>/dev/null | awk 'NR==2 {print $4}')
TMP_MOUNT=$(df -Pm "$TMPDIR" 2>/dev/null | awk 'NR==2 {print $1}')
echo "  TMPDIR=$TMPDIR（挂载点 $TMP_MOUNT，可用 ${TMP_AVAIL_MB:-?} MB）"
if [ -n "${TMP_AVAIL_MB:-}" ] && [ "$TMP_AVAIL_MB" -lt 3072 ]; then
  red "  ⚠️ 临时目录空间不足（${TMP_AVAIL_MB} MB）—— 下载一定失败！"
  echo "     这就是 'No space left on device' 的真正原因（跟磁盘总量无关）。"
  echo "     临时改指向大盘："
  echo "       export TMPDIR=/mnt/你的大盘/tmp && mkdir -p \$TMPDIR"
  echo "     或直接用本脚本的 --cache 参数（会自动设好 TMPDIR）："
  echo "       bash $APP_DIR/install_vps.sh --engine=$ENGINE --cache=/mnt/你的大盘"
  if [ "$FORCE" != "1" ]; then
    exit 1
  fi
fi

if [ "$ENGINE" = "camoufox" ]; then
  "$PY" -m pip install "camoufox[geoip]" --quiet
  # 已经装好就别重下 1.2GB —— 反复重跑安装脚本时能省十几分钟。
  PRE_INSTALLED="$(PS_APP_DIR="$APP_DIR" "$PY" - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PS_APP_DIR", "."))
try:
    from browser_engine import engine_status
    print("1" if engine_status().get("camoufox", {}).get("binary") else "0")
except Exception:
    print("0")
PY
)"
  if [ "$PRE_INSTALLED" = "1" ]; then
    grn "Camoufox 已安装（跳过下载）"
  else
    "$PY" -m camoufox fetch || true
    # **GitHub API 限流的自动回退。**
    # `camoufox fetch` 第一步是 _do_sync() → 打 api.github.com 查版本列表。
    # 未认证的 API 调用每小时只有 60 次，VPS 共享 IP 常年被打满 →
    #   403 Client Error: rate limit exceeded
    #   Synced 0 versions from 0 repos.
    #   v156.0.1-beta.36, the browser this camoufox release pairs with,
    #   is not in official's releases.
    # 但**真正的 zip 下载走 releases 地址，不受限流影响**，
    # 而且版本是硬锁定的（browser-pin.json），URL 完全确定 →
    # 用 fetch_camoufox.py 直接下载解压，绕开 API。
    if ! "$PY" - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PS_APP_DIR", "."))
try:
    from browser_engine import engine_status
    sys.exit(0 if engine_status().get("camoufox", {}).get("binary") else 1)
except Exception:
    sys.exit(1)
PY
    then
      ylw "  常规 fetch 未成功，尝试绕过 GitHub API 限流直接下载"
      PS_APP_DIR="$APP_DIR" "$PY" "$APP_DIR/fetch_camoufox.py" || true
    fi
  fi
  # **必须校验本体是否真的落地。**
  # camoufox CLI 遇到下载失败会打印 Error 但仍以 0 退出，
  # 只看退出码会把「下载失败」误判成「安装成功」。
  VERIFY="$(PS_APP_DIR="$APP_DIR" "$PY" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["PS_APP_DIR"])
try:
    from browser_engine import engine_status
    st = engine_status()
    print("1" if st.get("camoufox", {}).get("binary") else "0")
except Exception as exc:
    print("0")
    print(f"  (校验异常: {exc})", file=sys.stderr)
PY
)"
  if [ "$VERIFY" != "1" ]; then
    red "  ❌ Camoufox 浏览器本体没装上"
    echo ""
    echo "  最常见原因与对策："
    echo ""
    echo "  A) GitHub API 限流（最常见）"
    echo "     → 症状：403 rate limit exceeded / Synced 0 versions from 0 repos"
    echo "     → 绕过 API 直接下载（推荐，无需 token）："
    echo "       $PY $APP_DIR/fetch_camoufox.py"
    echo "     → 或者配 token（未认证 API 只有 60 次/小时，带 token 是 5000 次）："
    echo "       export GITHUB_TOKEN=ghp_xxxx && $PY -m camoufox fetch"
    echo ""
    echo "  B) ~/.cache/camoufox 是坏软链接（自环或断链）"
    echo "     → mkdir(exist_ok=True) 遇到自环会抛 FileExistsError"
    echo "     一键修复："
    echo "       rm -f $HOME/.cache/camoufox $HOME/.cache/ms-playwright"
    echo ""
    echo "  C) 临时目录空间不足（下载的 zip 落在 \$TMPDIR）"
    echo "     → 当前 TMPDIR=$TMPDIR（可用 ${TMP_AVAIL_MB:-?} MB）"
    echo "     修复：export TMPDIR=/大盘/tmp && mkdir -p \$TMPDIR"
    echo ""
    echo "  D) 网络问题"
    echo "     → 换个时间重试，或换机器"
    echo ""
    echo "  查看详细日志："
    echo "    cd $APP_DIR && $PY -m camoufox fetch"
    exit 1
  fi
  grn "Camoufox 就绪（浏览器本体已确认存在）"
else
  "$PY" -m pip install patchright --quiet
  "$PY" -m patchright install --with-deps chromium
  grn "Patchright Chromium 就绪（约 400MB）"
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
# TMPDIR 同样要写进去 —— camoufox 下载 zip 时用的是 tempfile，
# 落在 /tmp（很可能是小 tmpfs）就会 No space left on device。
set_env() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$APP_DIR/.env"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$APP_DIR/.env"
  else
    printf '%s=%s\n' "$key" "$val" >> "$APP_DIR/.env"
  fi
}
if [ -n "$CACHE_HOME" ]; then
  set_env XDG_CACHE_HOME "$CACHE_HOME"
  set_env PLAYWRIGHT_BROWSERS_PATH "$CACHE_HOME/ms-playwright"
  grn "已把缓存路径写入 .env"
fi
if [ -n "${TMPDIR:-}" ] && [ "$TMPDIR" != "/tmp" ]; then
  set_env TMPDIR "$TMPDIR"
  grn "已把 TMPDIR=$TMPDIR 写入 .env"
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
  # TMPDIR：camoufox 下载 zip 走 tempfile，默认落 /tmp（可能是小 tmpfs）
  if [ -n "${TMPDIR:-}" ]; then
    AGENT_CACHE_ENV="$AGENT_CACHE_ENV
Environment=TMPDIR=$TMPDIR"
  fi
  # 资源上限：浏览器只在跑挑战那几十秒吃内存，平时 agent 只有几十 MB。
  # 但 1 核 / 1GB 的小机器上，不加限制会直接把整机拖进 swap 抖动。
  AGENT_LIMITS="MemoryMax=700M
MemorySwapMax=1G
TasksMax=256"
  if [ "$LOW_MEMORY" = "1" ]; then
    # 小内存机器：给得更紧，宁可浏览器 OOM 重启也不要整机卡死
    AGENT_LIMITS="MemoryMax=600M
MemorySwapMax=1G
TasksMax=128
OOMPolicy=stop"
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
$AGENT_LIMITS
ExecStart=$VENV/bin/python $APP_DIR/agent/panstar_agent.py
Restart=always
RestartSec=5
TimeoutStopSec=20

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
    echo ""
    echo "  最常见原因（按概率排序）："
    echo "    1) 缺 flask 依赖 → 日志里有 ModuleNotFoundError: flask"
    echo "       修复：$PY -m pip install -r $APP_DIR/agent/requirements.txt"
    echo "    2) 浏览器本体没装上 → 日志里有 CamoufoxNotInstalled"
    echo "       修复：$PY -m camoufox fetch"
    echo "    3) 端口 3000 被占 → journalctl -u panstar-agent -n 20"
    echo ""
    echo "  实时日志：journalctl -u panstar-agent -f"
  fi
fi

# ---------------------------------------------------------------- 定时任务
step "安装定时任务"
# Debian/Ubuntu 精简安装里常常**没有 cron**，
# 直接 crontab 会报 "crontab: command not found"，而且定时任务根本没装上。
if ! command -v crontab >/dev/null 2>&1; then
  ylw "  系统没装 cron，先装"
  $SUDO apt-get install -y --no-install-recommends cron >/dev/null 2>&1
  if command -v crontab >/dev/null 2>&1; then
    grn "  cron 已安装"
    # 服务不一定要 enable（容器里没 systemd），但能起就起
    $SUDO systemctl enable cron >/dev/null 2>&1 || true
    $SUDO systemctl restart cron >/dev/null 2>&1 || true
  else
    red "  ❌ cron 装不上 —— 定时任务无法安装"
    echo "     手动排查： apt-get install cron"
    echo "     临时替代：用 systemd timer 跑 $APP_DIR/run_sign.sh"
  fi
fi

if command -v crontab >/dev/null 2>&1; then
  CRON_LINE="17 3 * * * $APP_DIR/run_sign.sh >> $APP_DIR/logs/cron.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "$APP_DIR/run_sign.sh"; then
    ylw "定时任务已存在，跳过"
  else
    ( crontab -l 2>/dev/null; echo "$CRON_LINE" ) | crontab -
    grn "已安装定时任务（每天 03:17）："
    echo "  $CRON_LINE"
  fi
else
  red "  ⚠️ 定时任务没装上（缺 crontab 命令）"
fi

step "完成"
echo
echo "接下来："
echo "  1) 编辑账号配置： nano $APP_DIR/.env"
echo "  2) 先手动跑一次： bash $APP_DIR/run_sign.sh --now"
echo "  3) 看日志：       tail -f $APP_DIR/logs/sign.log"
