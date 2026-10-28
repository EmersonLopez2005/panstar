

## 快速开始（Debian 12/13、Ubuntu 22.04+）

```bash
it clone https://github.com/EmersonLopez2005/panstar.git && cd panstar

# 装依赖 + 浏览器 + venv（Camoufox 约 1.3GB，下载慢是正常的）
bash install_vps.sh --agent

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
```

**签到一直失败时按这个顺序查：**

1. `python3 check_env.py` —— 出口 IP 是机房 IP 吗？浏览器能过挑战吗？
2. `curl http://127.0.0.1:3000/health` —— agent 活着吗？engine 对吗？
3. `logs/sign.log` 里 `[turnstile]` 那几行 —— 卡在哪条路径？
4. 出口 IP 不干净 → 租个住宅代理，填 `AGENT_PROXY`

## 常见问题

**Q：一定要 VPS 吗？** 本机也能跑，但需要装 camoufox + 有显示器或 Xvfb。
VPS 的好处是常驻 + 出口 IP 稳定（指纹不会天天变）。

**Q：会封号吗？** 签到频率跟真人一样（一天一次），profile 持久化保留指纹，
比每天用新环境安全得多。

**Q：需要多久跑一次？** 一天一次即可。定时任务已默认设为 03:17。

## 说明

- 本项目**不含打码服务**，Turnstile 由自带浏览器解决。
- 站点协议逆向仅用于个人账号的每日签到，请勿用于批量注册或刷分。
