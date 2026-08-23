# Admin 控制台（`admin/`）

React + Vite + TypeScript 单页应用，**只消费 `/admin/api/v1` 边界**。

它不持有任何凭据：会话完全依赖服务端下发的 HttpOnly Cookie，
`localStorage` / `sessionStorage` 里永远是空的（有测试断言）。
CSRF 令牌只存在 React 内存态，页面刷新后由 `GET /admin/api/v1/auth/session` 重新取回。

## 技术栈与命令

与 `miniapp/`（Node 内置 `node:test`）**不同**：这里用 Vitest + Playwright。

```bash
cd admin
npm install            # 首次；package-lock.json 已提交，安装结果可复现
npm test               # Vitest 单元/组件测试（当前 63 项通过）
npm run build          # tsc --noEmit + vite build
npm run dev            # Vite dev server，http://localhost:5173
npm run test:e2e       # Playwright，需要浏览器二进制与后端，见下文
```

## 开发态拓扑（重要）

**前后端必须都用 `localhost`，不要混用 `127.0.0.1`。**

会话 Cookie 是 `SameSite=Strict` 且 `Path=/admin`。对 Cookie jar 来说
`localhost` 和 `127.0.0.1` 是两个不同的主机，混用会导致 Cookie 不回传——
而且**只在开发环境出错**，很难排查。

`vite.config.ts` 已把 `/admin/api` 代理到 `http://localhost:8000`，
所以浏览器视角下前后端同站，Cookie 正常携带。

开发环境**不加 `Secure`**（浏览器会拒收 http 下的 Secure Cookie）；
staging / production 一定加。

## 手动验收清单

自动化测试已全绿（63 项 Vitest + 4 项 Playwright + 784 项 pytest），
但仍需要人工过一遍界面。请按顺序执行。

### 第 1 步：建一个 admin 账号

仓库没有 `create-admin` CLI（Phase 08 才有运维入口），先用一段脚本。
**在仓库根目录执行**，把密码换成你自己的：

```bash
mkdir -p tmp/admin-dev
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/admin-dev/admin.sqlite3 \
  .venv/bin/alembic upgrade head

.venv/bin/python - <<'PY'
from server.db import create_session_factory
from server.models import AdminUser
from server.services.admin_sessions import hash_password

USERNAME = "ops-admin"
PASSWORD = "换成你自己的密码"          # 不要用示例值

factory = create_session_factory("sqlite+pysqlite:///./tmp/admin-dev/admin.sqlite3")
with factory() as session:
    session.add(AdminUser(username=USERNAME, password_hash=hash_password(PASSWORD)))
    session.commit()
    print("created:", USERNAME)
PY
```

`password_hash` 存的是 Argon2id 串（`$argon2id$...`），明文不落库。

### 第 2 步：启动后端

```bash
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/admin-dev/admin.sqlite3 \
GRADER_DATA_DIR=./tmp/admin-dev/data \
GRADER_ADMIN_ORIGIN=http://localhost:5173 \
  .venv/bin/python -c 'import uvicorn; from server.config import ServerSettings; from server.main import create_app; uvicorn.run(create_app(ServerSettings()), host="localhost", port=8000)'
```

注意 host 是 `localhost` 而不是 `127.0.0.1`（见上文）。
`GRADER_ADMIN_ORIGIN` 必须与 Vite dev server 的地址完全一致，
否则所有写操作会被 Origin 校验拒绝（403）。

### 第 3 步：启动 Vite dev server

```bash
cd admin && npm run dev
```

打开 <http://localhost:5173/admin/login>。

### 第 4 步：逐页预期结果

| 页面 | 路径 | 预期 |
|---|---|---|
| 登录 | `/admin/login` | 用户名+密码表单。密码错误显示「用户名或密码不正确。」；**用不存在的用户名应显示完全相同的文案**（不泄漏账号是否存在）。连续错 5 次后显示「登录尝试过于频繁」（429），此时**即使输入正确密码也应被拒**。 |
| 总览 | `/admin/overview` | 登录后默认落地页。队列（排队/批改中/异常）、待验收、退款（待审批/失败）、Worker（在线/疑似离线/停用）、存储。**「最近备份」应显示「尚未启用（Phase 09）」——它不该编造一个备份时间。** |
| 订单 | `/admin/orders` | 跨用户订单列表。筛选条件写进 URL（可收藏）。查询框只接受**精确**值：订单号、用户公开 ID、支付交易号。点订单号进详情。 |
| 订单详情 | `/admin/orders/{id}` | 状态、用户公开 ID、金额、轮次与任务、附件（**只有逻辑名与大小，没有服务器路径**）、时间线、退款记录。底部红框「技术性退款」需**先填原因**才能点确认，且界面上没有金额输入框。 |
| 售后 | `/admin/aftersales` | 用户退款队列。`pending` 行有「批准退款 / 驳回…」；驳回**必须填原因**才能提交。`refund_failed` 行显示「重试退款」而不是「批准退款」。 |
| Worker | `/admin/workers` | 设备、平台/架构、版本、状态、当前任务、租约到期、心跳。三个按钮：停止接单 / 停用 / 恢复。页面顶部明确写着**这两个操作不会取消正在执行的任务**。 |
| 用户 | `/admin/users` | 按公开 ID 查询。显示累计支付、用户退款、**技术性退款（标注「不计入该用户指标」）**、本月次数、累计占比。**不显示 openid。** |
| 资金 | `/admin/funds` | 收款与退款汇总。**必须显示「尚未接入银行对账单……不声称任何款项已到账」**——不能声称钱已到账。 |
| 设置 | `/admin/settings` | 当前价格 + 「发布新价格版本」；七个运营参数可改。**页面上不出现任何密钥字段。** |
| 审计 | `/admin/audit` | 按动作/目标类型/操作者筛选。**没有编辑或删除按钮**，页面写明「只增不改不删」。 |

### 第 5 步：价格版本化人工验证（重要）

这一步验证「调价不影响已有报价」这个业务不变量：

1. 在小程序侧（或用 `curl` 走 `/api/v1/quotes`）建一个报价，记下金额。
2. 到管理台设置页把价格改成别的值，发布新版本。
3. 回去查那个**旧报价**，金额必须**没变**。
4. 再建一个**新报价**，金额应按新价格计算。

### 第 6 步：安全性人工抽查

1. 登录后按 F5 刷新 → **仍处于登录态**（Cookie 恢复会话）。
2. 浏览器 DevTools → Application → Local Storage / Session Storage → **两者都必须是空的**。
3. DevTools Console 执行 `document.cookie` → **看不到 `grader_admin_session`**（HttpOnly 生效）。
4. DevTools → Application → Cookies → `grader_admin_session` 的
   `HttpOnly` ✓、`Path=/admin`、`SameSite=Strict`；开发环境 `Secure` 应为空。
5. 点「退出」→ 回到登录页，且 Cookie 已被清除。
6. 退出后直接访问 `http://localhost:5173/admin/orders` → **被重定向回 `/login`**。

### 第 7 步（可选）：跑 Playwright

浏览器二进制约 95 MB，需联网下载：

```bash
cd admin
npx playwright install chromium
ADMIN_E2E_USERNAME=ops-admin ADMIN_E2E_PASSWORD='你的密码' npm run test:e2e
```

不设 `ADMIN_E2E_PASSWORD` 时整个 e2e 套件会被 skip（不会假装通过）。
它需要后端已在 `localhost:8000` 运行；Vite dev server 由 Playwright 自己拉起。

## 边界约定

- **只调 `/admin/api/v1/*`**，不碰 `/api/v1/*`（小程序域）与 `/worker/v1/*`（Worker 域），
  有测试断言每个请求的 URL 前缀。
- 代码里不得出现任何密钥、token、数据库连接串。
- 写操作（POST/PATCH/DELETE）必须带 `X-CSRF-Token`；读操作不带。
