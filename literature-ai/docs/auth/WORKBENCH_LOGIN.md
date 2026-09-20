# 工作台真实登录（2026-09-21 上线）

把 `https://dft.researchlife.top/login` 从"静态演示页"升级为真实登录入口，
并用会话鉴权取代了原来的全局 HTTP Basic 网关门。

## 1. 会话方式选择：HttpOnly Cookie（不是 Bearer token）

选 Cookie 的唯一决定性理由：**nginx 必须能校验"页面"请求**。
网关用 `auth_request` 子请求给每个页面与 `/api` 调用做鉴权，而
`localStorage` 里的 Bearer token 不会随浏览器导航自动发送，无法用于拦页面。
Cookie 还会被同源 `fetch`/`XHR` 自动携带，因此工作台前端几乎不用改。

代价是 Cookie 会被浏览器自动附带，所以必须防 CSRF —— 见第 3 节。

## 2. 账号来源：复用原有 htpasswd

| 项目 | 值 |
|---|---|
| 文件（宿主机） | `/opt/literature-ai/deploy/nginx/owner.htpasswd` |
| 挂载进 backend | `/etc/litai/owner.htpasswd`（只读，`LITAI_AUTH_HTPASSWD_FILE`） |
| 挂载进 owner-gateway | `/etc/nginx/.htpasswd`（保留原挂载，不再用于 Basic 鉴权） |
| 哈希算法 | `$apr1$`（Apache MD5 crypt，加盐）；也支持校验 `$1$` 与 `{SHA}` |

密码只以哈希形式存在，从不写明文；后端校验是纯标准库实现
（`backend/app/security/htpasswd.py`，已用 `openssl passwd -apr1` 对照验证）。
原有 Basic 密码在登录页继续可用，无需重置。

已经被拒绝的哈希形式：bcrypt（`$2y$`/`$2b$`，镜像里没有 bcrypt 依赖）与明文条目；
遇到这类条目会记 warning 并拒绝登录，请用 `set-password` 重新哈希。

### 增改账号（在服务器上执行，即时生效，无需重启容器）

```bash
python3 /opt/literature-ai/scripts/litai_auth_user.py list
python3 /opt/literature-ai/scripts/litai_auth_user.py add alice
python3 /opt/literature-ai/scripts/litai_auth_user.py set-password alice
python3 /opt/literature-ai/scripts/litai_auth_user.py verify alice
python3 /opt/literature-ai/scripts/litai_auth_user.py delete alice --yes
printf '%s\n' 'new-strong-password' | python3 /opt/literature-ai/scripts/litai_auth_user.py set-password alice --password-stdin
```

## 3. 接口与防护

| 接口 | 说明 |
|---|---|
| `POST /api/auth/login` | 账号+密码 → 200 + `Set-Cookie: litai_session`（HttpOnly）+ `litai_csrf` |
| `POST /api/auth/logout` | 撤销当前会话（Redis 吊销名单）+ 清 Cookie |
| `GET  /api/auth/me` | 当前身份；未登录 401 `not_authenticated` |
| `GET  /api/auth/verify` | **仅 nginx 内部子请求**使用；外部访问固定 404 |

防护机制：

- **CSRF 双提交**：`litai_csrf` Cookie（可读）+ `X-CSRF-Token` 请求头 + 会话载荷里的
  csrf 值三者必须一致，作用于所有带会话 Cookie 的 `POST/PUT/PATCH/DELETE`。
  前端在 `frontend/shared/topnav.js` 里给全站 `fetch`/`XHR` 自动补这个头。
- **Origin 校验**：写方法若带 `Origin` 且主机名不在允许集合（当前 Host +
  `LITAI_OAUTH_ISSUER` 主机 + `LITAI_AUTH_ALLOWED_ORIGINS`）内 → 403。
- **限流**：按 `IP+账号`（默认 8 次失败/15 分钟）与 `IP`（默认 30 次/15 分钟）计数，
  超限返回 429 + `Retry-After`；计数存 Redis db 2，重启不丢。
- **不泄露账号是否存在**：账号不存在与密码错误返回完全相同的 401
  `{"detail":"invalid_credentials"}`，并对不存在的账号做等时长的假哈希校验。
- **日志**：失败/成功只记用户名与 IP，永不记密码或哈希。
- **会话有效期与续期**：普通登录 12 小时（浏览器会话 Cookie，关浏览器即失效）；
  勾选"记住我" 30 天（持久 Cookie）。有任何 `/api` 流量且剩余时间过半时自动
  续签（滑动续期），因此活跃用户不会中途掉线。
- **会话绑定密码**：会话令牌内含当前 htpasswd 哈希指纹（`pv`），
  改密码/删账号会立刻让该账号所有旧会话失效；轮换
  `.env` 的 `LITAI_AUTH_SESSION_SECRET` 则让**所有**会话失效。

## 4. nginx 行为（`deploy/nginx/owner.conf.template`）

- 公开：`/login`、`/login/`、`/api/auth/*`、`/api/health`、`/mcp`（Bearer 自鉴权）、
  `/oauth/`、`/.well-known/`。
- 其余页面与 `/api`：`auth_request /_litai_auth`（内部子请求 → 后端 `/api/auth/verify`）。
  失败时页面 **302 到 `/login`**，接口 **401 JSON**；两者都**不带** `WWW-Authenticate`，
  避免 Chromium 弹原生登录框（曾经导致页面卡死并被回滚的坑）。
- `/api/auth/verify` 外部固定 404；`/docs`、`/redoc`、`/openapi.json` 仍为 404。
- `absolute_redirect off; port_in_redirect off;`：容器内监听 8080，但公网是
  https/443，必须让 nginx 自身产生的 302 用相对路径，否则会跳到
  `http://<host>:8080/login`（混合内容/打不开）。
- `/api/share/` 单独一个 location：仍要求会话，但保留后端自己的 401（无效分享令牌），
  不会被改写成登录页跳转。

## 5. 配置项（`.env` → `docker-compose.yml` → backend）

```
LITAI_AUTH_ENABLED=true
LITAI_AUTH_HTPASSWD_FILE=/etc/litai/owner.htpasswd
LITAI_AUTH_SESSION_SECRET=<32 字节 hex，轮换即踢掉所有会话>
LITAI_AUTH_SESSION_TTL_HOURS=12
LITAI_AUTH_SESSION_REMEMBER_DAYS=30
LITAI_AUTH_COOKIE_SECURE=true
LITAI_AUTH_REDIS_URL=redis://redis:6379/2
LITAI_AUTH_LOGIN_WINDOW_SECONDS=900
LITAI_AUTH_LOGIN_MAX_FAILURES_PER_IP_USER=8
LITAI_AUTH_LOGIN_MAX_FAILURES_PER_IP=30
```

改 `.env` 后必须 `docker compose up -d --no-deps --force-recreate backend`（`restart` 不重读）。

## 6. 测试

```bash
# 单元 + 接口测试（纯 unit，不需要 PostgreSQL）
docker exec -e PYTHONPATH=/app -w /app literature-ai-backend-1 \
  python -m pytest tests/test_workbench_auth.py -q -p no:cacheprovider
```

## 7. 回滚（逐文件，先备份再改）

改动前的原件保存在 `/home/2401liyuhao/litai_auth_backup_20260921/`
（`nginx/owner.conf.template`、`nginx/rendered_default.conf.before`、`compose/docker-compose.yml`、
`env/env.orig`、`backend/main.py`、`backend/config.py`、`frontend/...`、`deliverables/...`）。

```bash
BK=/home/2401liyuhao/litai_auth_backup_20260921
cd /opt/literature-ai

# 1) 还原 nginx 网关（回到全局 Basic）
cp -p $BK/nginx/owner.conf.template deploy/nginx/owner.conf.template
docker compose up -d --no-deps --force-recreate owner-gateway

# 2) 还原 compose（去掉 htpasswd 挂载与 auth 环境变量）
cp -p $BK/compose/docker-compose.yml docker-compose.yml
cp -p $BK/env/env.orig .env

# 3) 还原后端与前端
cp -p $BK/backend/main.py       backend/app/main.py
cp -p $BK/backend/config.py     backend/app/config.py
cp -p $BK/frontend/pages/pelican_bike/index.html frontend/pages/pelican_bike/index.html
cp -p $BK/frontend/pages/literature_library/api.js frontend/pages/literature_library/api.js
cp -p $BK/frontend/shared/topnav.js frontend/shared/topnav.js
cp -p $BK/frontend/shared/topnav.css frontend/shared/topnav.css
cp -p $BK/deliverables/pelican-db-login.html deliverables/pelican-db-login.html

# 4) 新增文件若也要撤掉（逐个确认后删除）
#    backend/app/api/auth.py
#    backend/app/security/htpasswd.py
#    backend/app/security/session_auth.py
#    backend/app/security/rate_limit.py
#    backend/tests/test_workbench_auth.py
#    scripts/litai_auth_user.py

# 5) 重建后端
docker compose up -d --no-deps --force-recreate backend
```

备份目录里另有 `*.pristine` 文件（来自 git 源的改动前版本），可用于逐字节比对：
`frontend/shared/topnav.js.pristine`、`frontend/pages/literature_library/api.js.pristine`、
`frontend/pages/literature_library/index.html.pristine`。
`frontend/pages/literature_library/index.html` 本次未修改。

## 8. 已知取舍

- 账号管理走服务器脚本（L3 权限），不提供网页注册/改密页面。
- 会话是签名令牌 + Redis 吊销名单，不是数据库会话表；优点是不依赖 DB、
  后端重启不掉线，代价是吊销名单需要 Redis（Redis 不可用时退化为进程内名单，
  logout 只对当前进程立即生效，密码指纹仍然兜底）。
- 登录限流按 `CF-Connecting-IP`/`X-Forwarded-For` 左值取客户端 IP；
  若将来直连后端绕过 cloudflared，应同时收紧 `forwarded_allow_ips`。
