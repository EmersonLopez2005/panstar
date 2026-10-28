#!/bin/sh
# 容器入口：默认用 Xvfb 起一个虚拟显示器，让浏览器以"有头"方式运行。
# 这不是为了好看——Cloudflare 能认出 headless shell，
# 无头模式下 Turnstile 过不去（实测 Error 600010）。
set -e

if [ "${AGENT_HEADLESS}" = "true" ] || [ "${AGENT_HEADLESS}" = "1" ]; then
    echo "[entrypoint] 无头模式启动（注意：CF 大概率会拦）"
    exec python panstar_agent.py "$@"
fi

echo "[entrypoint] Xvfb 有头模式启动"
exec xvfb-run -a --server-args="-screen 0 1440x900x24" \
    python panstar_agent.py "$@"
