# PanstarCloud 自动签到（独立 VPS 项目）

给 [panstar.ai](https://panstar.ai) 做每日签到，**自带浏览器，不依赖打码服务**。

## 为什么不用打码服务

站点签到强制要 Cloudflare Turnstile token（`checkinSecurityState: REQUIRED`），实测：

| 方案 | 结果 |
|---|---|
| 打码服务（CloudFreed） | 容器里能 createTask，但 90s 拿不到 token；且 token 会因来源 IP/UA 不匹配被判无效 |
| 普通 Playwright/Chrome 自动化 | **Turnstile Error 600010**（widget already executing），CF 直接判 bot |
| **Camoufox + 有头 + 持久化 profile** | ✅ 这是本项目采用的方式 |

所以这个项目自带一个常驻的浏览器服务，用**反检测浏览器（Camoufox，Firefox 定制版）**
在真实页面里把 Turnstile 跑出来 —— token 的来源与请求方天然一致。

> 你截图里真浏览器签到成功（26 经验），也印证了这点：站点没坏，
> 坏的是自动化环境被 Cloudflare 认出来了。

## 三层结构

```
┌─────────────────────────────────────────┐
│  run_sign.sh（cron 包装）                 │  ← cron 只调这一个
├─────────────────────────────────────────┤
│  panstar_sign.py（主脚本）                │
│    1. 取令牌：refreshToken 续期 → 密码登录 │
│    2. 查今日状态：已签到就直接返回          │
│    3. 签到：免ticket直签 → 浏览器 → 带ticket │
├─────────────────────────────────────────┤
│  panstar_client.py（站点 API）             │  AES-GCM 解密 + Sec-Fetch 来源头
│  turnstile_solver.py（取 token）          │  浏览器代理 → 本地浏览器
├─────────────────────────────────────────┤
│  agent/（浏览器代理，常驻）                │
│    panstar_agent.py    Flask HTTP 服务     │
│    browser_engine.py   反检测浏览器适配层   │
└─────────────────────────────────────────┘
```

## 快速开始（Debian 12/13、Ubuntu 22.04+）

```bash
git clone <这个仓库> panstar-autosign && cd panstar-autosign

# 装依赖 + 浏览器 + venv（Camoufox 峰值约 2.6GB，下载慢是正常的）
bash install_vps.sh --agent

# 空间紧张时：把浏览器内核放到另一块盘 + 跳过中文字体
bash install_vps.sh --agent --cache=/mnt/大硬盘 --no-cjk-fonts

# 体检：看出口 IP、浏览器能不能过挑战
python3 check_env.py

# 填账号
nano .env

# 手动跑一次
bash run_sign.sh --now

# 看结果
tail -f logs/sign.log
```

## 配置（.env）

```bash
# 账号（多账号用 & 分隔）
PS_USER_PASS=你的邮箱:你的密码

# 浏览器代理地址（install_vps.sh --agent 会自动起）
LOGIN_AGENT_URL=http://127.0.0.1:3000
LOGIN_AGENT_TOKEN=安装脚本会打印出来，填这里
```

完整项见 `.env.example`。

## 站点协议备忘（全部实测过）

| 项 | 值 |
|---|---|
| 业务路径前缀 | `/api/ecs/...`（**不是** `/api/`） |
| 刷新令牌 | `/api/s0/auth/refresh-token`（**不带 ecs**） |
| 响应加密 | AES-GCM，头 `x-api-encrypt: 1` + `x-api-encrypt-iv` |
| 解密密钥 | `26lAIOdVLW74vJFTawTAiA79kXTtdlibFh49gt+j5Zk=`（前端 bundle 硬编码） |
| accessToken TTL | 15 分钟（必须靠 refreshToken 续期） |
| Turnstile sitekey | `0x4AAAAAAD4S6pSwBl0NuILe` |
| Turnstile action | `points_checkin`（**必须传**，否则 token 可能被判无效） |
| Turnstile 模式 | `execution=execute` + `invisible=true` + `retry=never` |

### 错误码

| code | messageKey | 含义 | 处理 |
|---|---|---|---|
| 10 | Token_Expired | 令牌过期 | 用 refreshToken 续期 |
| 52 | USER_PASSWORD_ERROR | 密码错 | 不重试，告警 |
| 718 | POINTS_CHECKIN_SECURITY_REQUIRED | 需要 Turnstile | 走浏览器流程 |
| 719 | POINTS_CHECKIN_PREFLIGHT_FAILED | preflight 失败 | token 无效 |
| 720 | POINTS_CHECKIN_TICKET_INVALID | ticket 无效 | 重新取 token |
| 741 | TRUSTED_BROWSER_REQUEST_REQUIRED | 来源校验失败 | **加 `Sec-Fetch-*` 头**（本项目已处理） |

### 两个容易踩的坑

1. **用错路径前缀 → 全部返回 code=10**，看起来像"没登录"，极具误导性。
2. **POST 缺 `Sec-Fetch-Site` → 403 code=741**。这是服务端的可信浏览器校验，
   加上 `Sec-Fetch-Dest: empty` / `Sec-Fetch-Mode: cors` / `Sec-Fetch-Site: same-origin` 即可。

## 目录结构

```
panstar-autosign/
├── panstar_sign.py         主脚本
├── panstar_client.py       站点 API 客户端（解密/登录/签到）
├── turnstile_solver.py     Turnstile token 获取（浏览器代理 → 本地浏览器）
├── browser_engine.py       反检测浏览器适配层
├── check_env.py            部署体检
├── install_vps.sh          一键安装
├── run_sign.sh             cron 包装（带锁/心跳/预演）
├── cleanup_browsers.sh     清理多余浏览器内核
├── cleanup_disk.sh         磁盘清理（apt缓存/旧内核/Docker镜像/日志）
├── agent/
│   ├── panstar_agent.py    浏览器代理 HTTP 服务
│   ├── browser_engine.py   （同上，agent 侧副本）
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── entrypoint.sh
├── logs/                   签到日志
├── data/                   令牌缓存 + 浏览器 profile
└── .env                    配置（别提交）
```

## 排错

```bash
# 只看环境，不实际签到
bash run_sign.sh --dryrun

# 看上次跑的时间（心跳）
cat logs/last_run.txt

# agent 日志
journalctl -u panstar-agent -n 100

# agent 健康检查
curl http://127.0.0.1:3000/health

# 磁盘清理 + 空间体检（会扫描全盘 Top 15，找出空间到底在哪）
bash cleanup_disk.sh            # 只报告
bash cleanup_disk.sh --clean    # 真删
bash cleanup_disk.sh --aggressive  # 真删 + 删未使用的 Docker 镜像
```

## 磁盘不够怎么办

Camoufox 的真实开销是 **峰值约 2.6GB**（下载包 ~1.2GB 和解压后的 ~1.2GB 会同时存在），
不是 1.3GB。按下面顺序处理：

| 办法 | 命令 | 效果 |
|---|---|---|
| 定位大头 | `bash cleanup_disk.sh` | 扫全盘 Top 15，直接告诉你空间在哪 |
| 清理 | `bash cleanup_disk.sh --clean` | apt 缓存、旧内核、Docker 悬空镜像、journald |
| **换大盘放内核** | `bash install_vps.sh --cache=/mnt/大硬盘` | **最有效**，浏览器内核挪到另一块盘 |
| 换轻量引擎 | `bash install_vps.sh --engine=patchright` | 约 400MB，但过 CF 成功率较低 |
| 省中文字体 | `bash install_vps.sh --no-cjk-fonts` | 省约 250MB（页面可能显示方框） |
| 硬装 | `bash install_vps.sh --force` | 跳过空间检查（可能中途失败） |

> `--cache=` 用「软链接 + `XDG_CACHE_HOME`」双保险实现：
> Camoufox 认 `XDG_CACHE_HOME`，Playwright/Patchright 认 `PLAYWRIGHT_BROWSERS_PATH`。
> 路径会同时写进 `.env` 和 systemd 单元，cron 环境下也能找到。

**签到一直失败时按这个顺序查：**

1. `python3 check_env.py` —— 磁盘够吗？出口 IP 是机房 IP 吗？浏览器能过挑战吗？
2. `curl http://127.0.0.1:3000/health` —— agent 活着吗？engine 对吗？
3. `logs/sign.log` 里 `[turnstile]` 那几行 —— 卡在哪条路径？
4. 出口 IP 不干净 → 租个住宅代理，填 `AGENT_PROXY`

### 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `No space left on device` | 磁盘满，Camoufox 下到一半失败 | `bash cleanup_disk.sh --clean`，然后 `python -m camoufox fetch` 重装 |
| 清理完磁盘一点没释放 | 大头不在 apt/Docker/日志里 | `bash cleanup_disk.sh` 看全盘 Top 15，或 `du -xh --max-depth=1 / \| sort -h \| tail -15` |
| `没有可用的浏览器引擎` | pip 装了包但浏览器本体没下载 | `python -m camoufox fetch`（约 1.2GB） |
| 装好了却说找不到浏览器 | `--cache=` 装到了大盘，但 cron 环境没带上变量 | 重新跑 `install_vps.sh --cache=...`（会写进 `.env`），或直接 `export XDG_CACHE_HOME=...` |
| `[agent] 不可用` | agent 没启动 | `systemctl start panstar-agent` 或 `cd agent && docker compose up -d` |
| `Task was destroyed but it is pending!` | 浏览器启动失败的 asyncio 残留噪音 | 已静音，不是错误 |
| `ts: unbound variable` | 旧版 `run_sign.sh` 的 bug | 升级到最新版（已修） |
| 一直报 `需要 Turnstile` 但浏览器在跑 | 出口 IP 被 Cloudflare 判高风险 | 换 IP 或挂 `AGENT_PROXY` |

## 常见问题

**Q：一定要 VPS 吗？** 本机也能跑，但需要装 camoufox + 有显示器或 Xvfb。
VPS 的好处是常驻 + 出口 IP 稳定（指纹不会天天变）。

**Q：会封号吗？** 签到频率跟真人一样（一天一次），profile 持久化保留指纹，
比每天用新环境安全得多。

**Q：需要多久跑一次？** 一天一次即可。定时任务已默认设为 03:17。

## 说明

- 本项目**不含打码服务**，Turnstile 由自带浏览器解决。
- 站点协议逆向仅用于个人账号的每日签到，请勿用于批量注册或刷分。
