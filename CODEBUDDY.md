# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

## 项目性质

数学竞赛（联赛二试 / CMO / IMO）解答 PDF 的 AI 批改服务：**微信小程序 + 云服务端 + 分布式 Worker**。

旧版单机批改器已于 2026-08-09迁出到 `~/Desktop/旧的小程序`，**本仓库只剩新架构**。

| 目录 | 角色 | 状态 |
|---|---|---|
| `server/` | 服务端 | Phase 01（配置/DB/状态机/模型/迁移/健康检查）+ Phase 02（登录/报价/支付回调/订单）+ Phase 03（Worker 控制面）+ Phase 04（bundle 下载端点）+ Phase 05（售后/退款/scheduler/ETA）已落地 |
| `worker/` | 跨平台 Worker 守护进程 | Phase 03 控制面 + Phase 04 批改运行时已落地：协议客户端、守护循环、租约续期、CLI、`LegacyCodexRuntime`、`worker/platforms/` 原生进程适配器、`worker/runtime/doctor.py` 八项环境自检、`worker/runtime/workspace.py` 隔离工作区。`FakeGrader` 保留为演示与测试基线 |
| `.agents/skills/olympiad-grader/` | 批改 Skill、评分口径、排版脚本、字体 | 保留；`worker/runtime/workspace.py` 在每个任务里复制进 Worker 的 `input/` 目录 |
| `miniapp/` | 微信原生小程序 | Phase 06 已落地：登录、三步创建向导、订单列表/详情、结果下载、验收/复核/退款。`npm test` 99 项通过（Node 内置 `node:test`） |
| `admin/` | React/Vite/TS 管理控制台 | Phase 07 已落地：Argon2id + Cookie 会话 + CSRF、九条路由、七个域。`npm test` 64 项通过（**Vitest**，不是 `node:test`），另有 4 项 Playwright e2e |
| `ops/` | 目标目录 | **尚不存在**，Phase 08–09 才创建 |

进度事实来源（Phase 07 完成时）：`pytest -q` **788 项通过 + 6 项跳过**
（`tests/server/` 651 通过 2 跳过、`tests/worker/` 134 通过 2 跳过、`tests/integration/` 3 通过 2 跳过），
外加 `miniapp/` 的 **99 项** Node 测试与 `admin/` 的 **64 项** Vitest 测试（+4 项 Playwright e2e，已在真实 Chromium 跑过）。
跳过的 6 项都需要外部环境：需真实 MySQL 的并发领取测试（2 项）与 scheduler advisory lock
测试（2 项），以及按宿主平台跳过的 Linux / Windows 进程终止 smoke 测试（2 项）。
数据库迁移 head：`0006`——Phase 07 新增 `admin_sessions`（0005）与 `operational_settings`（0006）。
Phase 05 的售后/退款/scheduler 测试已在一次性 MySQL 8.4 实例上验证（88 项通过）。
除已落地部分之外的一切都还是计划。

**不要把计划文档里的目录、接口、类当成已实现代码引用。**

### 旧版批改器的位置

`~/Desktop/旧的小程序` 是已验证批改实现的完整快照（57 项测试通过），含 `app/`、旧版测试、
`.agents/` 副本、`data/jobs/`、`archive/`、`启动批改.command`。它**不是 git 仓库**，也不随本仓库演进。

- Phase 04 已把批改链路搬到 Worker：`worker/runtime/legacy/` 下保留旧实现的逐字副本，
  仅在已验证 bug 时改动（见 `.codebuddy/rules/legacy-grader-boundaries.md`）。
- **`server/` 不得 import `app` 包**——该包已不在本仓库，`tests/server/test_pdf_adapter.py` 会断言这一点。
- PDF 校验已内化为 `server/adapters/pdf.py`（逻辑与旧 `app/pdf_utils.py`逐行一致）。
- `.agents/skills/` 两边各一份，内容迁移时一致；**调整评分口径要同步两边**。

## 常用命令

所有命令在仓库根目录执行。

```bash
# 安装开发环境
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

### 测试

```bash
.venv/bin/python -m pytest -q                # 全部（当前 788 通过 + 6 跳过）
.venv/bin/python -m pytest tests/server -q         # 服务端（651 通过 + 2 跳过）
.venv/bin/python -m pytest tests/worker -q         # Worker（134 通过 + 2 跳过）
.venv/bin/python -m pytest tests/integration -q    # 跨组件（3 通过 + 2 跳过）
.venv/bin/python -m pytest tests/server/test_states.py -q          # 单文件
.venv/bin/python -m pytest tests/server/test_states.py::test_v2_cannot_create_third_round -q   # 单测试
.venv/bin/python -m pytest -q -k "lease or refund" # 关键字筛选
```

小程序前端测试（不需要微信开发者工具，纯 Node 即可）：

```bash
cd miniapp && npm test            # 当前 99 项通过；Node 内置 node:test，无第三方框架
```

Admin 前端测试（**Vitest，与 miniapp 的 node:test 不同**）：

```bash
cd admin && npm test              # 当前 64 项通过
cd admin && npm run build         # tsc --noEmit + vite build
cd admin && npm run test:e2e      # Playwright；需 npx playwright install chromium 与后端
```

测试默认使用 `tmp_path` 和 SQLite：**不需要 MySQL，不会发起真实 Codex 调用，不会花钱**。

`tests/integration/test_mysql_job_claim.py` 与 `tests/server/test_scheduler_lock.py`
的 MySQL 用例需要真实 MySQL 8，缺 `GRADER_TEST_MYSQL_URL` 时会被 skip。**SQLite 会静默忽略 `FOR UPDATE` 且只允许单写者，
所以行锁正确性无法在 SQLite 上证明**——不要用 SQLite 上的多线程去"证明"原子 claim：

```bash
GRADER_TEST_MYSQL_URL="mysql+pymysql://root@127.0.0.1:3306/grader_test" \
  .venv/bin/python -m pytest tests/integration -q
```

仓库没有配置 linter / formatter / type checker，`pytest` 是唯一自动化关卡。

### 数据库迁移

Alembic **只从进程环境变量**读 `GRADER_DATABASE_URL`，不会加载 `.env`（与应用运行时不同，容易踩坑）：

```bash
mkdir -p tmp/server-data
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/server-dev.sqlite3 .venv/bin/alembic upgrade head
GRADER_DATABASE_URL=... .venv/bin/alembic revision --autogenerate -m "描述"
```

当前 head 是 `0006`。改 schema 时新增 migration，不要回写 `0001`–`0006`。

### 启动服务端（还没有 CLI / systemd 入口）

```bash
.venv/bin/python -c 'import uvicorn; from server.config import ServerSettings; from server.main import create_app; uvicorn.run(create_app(ServerSettings()), host="127.0.0.1", port=8000)'
curl http://127.0.0.1:8000/health/live    # {"status":"ok"}
curl http://127.0.0.1:8000/health/ready   # 验证 DB 连接 + data_dir 可写
```

## 服务端架构（`server/`）

分层严格，FastAPI 与数据库不侵入领域逻辑：

- `config.py` — `ServerSettings`（pydantic-settings，前缀 `GRADER_`，自动读根目录 `.env`）。三个敏感字段 `database_url` / `session_secret` / `worker_shared_key` 被包成 `SecretStr` 再解包校验，且 `ValidationError` 会被 `mode="wrap"` 校验器重建为脱敏版本——**修改这些字段时必须保证报错里不会泄漏原值**，`tests/server/test_config.py` 大量断言此行为。`production` 环境拒绝非 `mysql+pymysql://` 的 URL。`.env.example` 必须覆盖全部配置项（有测试断言）。
- `db.py` — `create_session_factory(url)`；SQLite 分支额外传 `check_same_thread=False`。
- `domain/states.py` — 纯函数状态机，无 FastAPI / DB 依赖。`ORDER_TRANSITIONS` / `JOB_TRANSITIONS` 是 `MappingProxyType` + `frozenset`，不可变。**任何订单或批改任务的状态变更必须经过 `require_order_transition` / `require_job_transition`**。
- `models/` — 按业务责任分 5 个模块（`accounts` / `orders` / `payments` / `workers` / `audit`），共 15 张表。统一约定：UUID 字符串主键（`String(36)`）、金额用整数「分」、时间用 `base.py` 的 `UTCDateTime`（绑定时要求 tz-aware、存库转 naive UTC、读出补回 UTC）。
- `adapters/` — 三个可替换 seam（`auth` / `payments` / `files`）+ `pdf.py`。fake 与生产实现通过配置切换。
- `services/` — 事务性用例（`sessions` / `files` / `quotes` / `payments` / `orders` / `workers` / `leases` / `results`）。
- `api/` — 小程序路由、支付回调与 Worker 控制面路由；`dependencies.py` 提供 `CurrentUser` / `DatabaseSession` / `Settings`，`worker_dependencies.py` 提供 `CurrentWorker` / `SharedKeyGuard`。
- `main.py` — `create_app(settings)` 工厂；`lifespan` 负责 `engine.dispose()`。没有全局 app 单例。

状态机的业务含义：订单一次交付为 V1，只允许一次复核（V2），`V2_DELIVERED` 不能再回 `V2_QUEUED`；任何非终态都可以走 `REFUND_PENDING`。

### 已实现的小程序 API（Phase 02）

| 路径 | 说明 |
|---|---|
| `POST /api/v1/auth/login` | 测试账号登录；**仅非 production 注册** |
| `GET /api/v1/me` | 当前用户 |
| `POST /api/v1/quotes`、`GET /api/v1/quotes/{id}` | PDF 报价与读取 |
| `POST /api/v1/payments/prepay` | 预支付意图 |
| `GET /api/v1/orders`、`GET /api/v1/orders/{id}` | 自己的订单列表与详情 |
| `POST /api/v1/payments/{id}/simulate-success` | 假支付；**仅非 production 注册** |
| `POST /callbacks/fake/pay` | 假支付回调；**仅非 production 注册** |

### 已实现的 Worker API（Phase 03 + Phase 04）

共享密钥 `Authorization: Bearer` + 独立 `X-Worker-ID`。**这些路由在所有环境都注册**
（含 production），它们不是 fake 适配器，不受 `FAKE_ADAPTER_ENVIRONMENTS` 门禁影响。

| 路径 | 说明 |
|---|---|
| `POST /worker/v1/register` | 注册；同一 `installation_id` 始终返回同一 `worker_id` |
| `POST /worker/v1/heartbeat` | 上报存活与阶段，可顺带续租 |
| `POST /worker/v1/jobs/lease` | 领取一个任务；长轮询上限 25 秒，`Prefer: wait=0` 立即返回 204 |
| `POST /worker/v1/jobs/{id}/ack` | `leased` → `running` |
| `POST /worker/v1/jobs/{id}/renew` | 用服务端时间续租，只接受 `running`/`uploading` |
| `POST /worker/v1/jobs/{id}/result/uploads` | 换单次使用上传凭证，`running` → `uploading` |
| `PUT /worker/v1/jobs/{id}/result/{kind}` | 上传 `result_json`/`result_pdf` 到按租约隔离的暂存区 |
| `POST /worker/v1/jobs/{id}/result/commit` | 事务性交付；重复提交幂等返回 `already_committed` |
| `GET /worker/v1/jobs/{id}/bundle/{kind}` | Phase 04：按租约绑定的下载令牌取 `source`/`reference` PDF，令牌随领取重新生成、租约回收失效 |

复核、退款与 Admin 退款审批已在 Phase 05 落地，见下文。

### 已实现的售后 API（Phase 05）

小程序认证域，归属从会话推导。三个动作互斥：每个都用「带状态谓词的条件 UPDATE」
抢占状态跃迁，并发时恰好一个成功、另一个 409，Appeal 与 Refund 不会同时存在。

| 路径 | 说明 |
|---|---|
| `POST /api/v1/orders/{id}/accept` | 验收，进入终态 `accepted` |
| `POST /api/v1/orders/{id}/review` | 一次复核：建第2 轮 + `queued` 任务，复用同一份不可变PDF |
| `POST /api/v1/orders/{id}/refund` | 全额退款；符合策略立即执行，否则等 Admin 审批 |

订单详情新增 `available_actions`（服务端权威、仅作提示）、`appeal_text`、`eta`。
V2 **没有**复核端点（返回 409）；退款金额一律取订单已付金额，不接受客户端传入。

### 已实现的结果下载 API（Phase 06）

小程序认证域。**在所有环境注册**——交付批改结果是真实功能，不是假适配器，
因此不受 `FAKE_ADAPTER_ENVIRONMENTS` 门禁影响。

| 路径 | 说明 |
|---|---|
| `GET /api/v1/orders/{id}/rounds/{n}/result/{kind}` | 取 `result_pdf` / `result_json`，流式返回 |

**刻意不用「换令牌再下载」两段式**：短期令牌是被缓存的授权决定，而
`orders.downloads_revoked_at` 恰恰是不能被缓存的决定——退款后必须立刻失效。
文件由本应用从本地磁盘直接提供（不经 CDN、不用预签名对象存储），没有需要委托的对象，
所以每次请求都重新校验会话凭据。`wx.downloadFile` 支持请求头，前端用同一个 session token 鉴权。

鉴权三项全部在同一次请求内校验：归属（在 SQL 层 JOIN 强制，非归属返回 **404** 而非 403，
避免确认他人订单存在）、`downloads_revoked_at is None`（否则 **410 Gone**，且在定位文件之前检查）、
轮次已交付且有对应产物（否则 404）。`output/internal/` 的中间产物永不暴露。

### 已实现的 Admin 退款 API（Phase 05）

**第三个独立认证域**：`Authorization: Bearer <GRADER_ADMIN_SHARED_KEY>` + `X-Admin-ID`
（`admin_users` 中一行存活记录）。密钥先 sha256 再 `hmac.compare_digest`。
和 `/worker/v1/*` 一样**在所有环境注册**——退款审批必须能在生产用，不是假适配器。

| 路径 | 说明 |
|---|---|
| `POST /admin/api/v1/refunds/{id}/approve` | 批准人工退款，走同一套幂等执行 |
| `POST /admin/api/v1/refunds/{id}/reject` | 驳回：订单回 `accepted`，**保留下载权** |
| `POST /admin/api/v1/refunds/technical` | 技术性退款，绕过用户策略且不计入用户指标 |

Phase 07 已完成替换：`server/` 里**不再有任何代码用 `admin_shared_key` 做认证**
（字段保留但废弃，Phase 08 删除）。认证仍要求真实 `admin_users` 行，
所以 `AuditLog.actor_id` 记录的是真人，Phase 05 的旧审计记录依然有效。

### 已实现的 Admin API（Phase 07）

七个域共 19 条路由，全部要求 Cookie 会话；写操作还要 `X-CSRF-Token` + 匹配的 `Origin`。
详见 README。要点：

- `GET /admin/api/v1/overview` 指标取自**同一次一致性快照**，不分多次查询拼凑。
- `GET /admin/api/v1/orders` 跨用户搜索，**刻意没有归属过滤**（这是管理面，
  不要把小程序的 ownership 约束抄过来）；keyset 分页；精确匹配而非 LIKE。
- 响应里**绝不出现** `relative_path`、`openid`、`installation_id`、任何密钥或数据库 URL。
  附件只报逻辑名与大小。
- `drain` / `disable` **只改 `workers.status`**，不动 `grading_jobs`、不清 `current_job_id`。
  `NON_LEASABLE_STATUSES` 是租约拒发的判据；按状态汇总时用 `ALL_WORKER_STATUSES` 遍历，
  否则新增状态会让那些 Worker 从面板上消失。
- `drain` 对已 `disabled` 的 Worker 返回 409，不把硬停降级成计划下线。
- 退款审批**复用 `RefundService`**，不存在第二条退款代码路径。
- `PATCH /settings` 走 allow-list（`EDITABLE_SETTINGS`）+ 逐项范围校验，
  **两层守卫各自独立有测试**（请求模型 `extra="forbid"` 与服务层检查）。
- 调价只**新建 `price_rules` 版本**，绝不回写 `QuoteSession.quoted_amount_cents`。
- 审计**只增**：`AuditLog` 没有 update/delete 路由。

### Phase 05 调度进程

```bash
.venv/bin/python -m server.scheduler.main# 取到 advisory lock 才开始，默认 20 秒一轮
```

七个幂等任务：`release_unacknowledged_leases` / `mark_expired_running_leases` /
`auto_accept_expired_orders` / `delete_expired_quotes` / `delete_expired_order_files` /
`retry_failed_refund_queries` / `verify_backup_freshness`（**占位**，真实备份是 Phase 09，
当前返回 `skipped`）。单例靠 MySQL 具名 advisory lock `grader-scheduler` 保证；
SQLite 上退化为空操作并报告 `enforced = False`，不假装在保护生产。

## 必须守住的安全不变量

这些都有对应测试，改动相关代码时不要破坏：

**环境门禁**：`server/main.py` 的 `FAKE_ADAPTER_ENVIRONMENTS` 控制假登录、假支付、假回调三组路由。
production 下它们**不注册、不进 OpenAPI、请求返回 404**。新增任何 fake 适配器都要纳入这个门禁。

**认证**：原始 session token 只返回一次，库里只存 sha256；小程序 token / Worker Shared Key / Admin Cookie
是三个独立认证域，用后两者访问 `/api/v1/me` 必须 401；过期与撤销会话被拒；错误信息不泄漏 token、
数据库 URL 或密钥。并发首次登录靠 savepoint + `users.openid` 唯一约束恢复，不能500。

**Worker 认证**：共享密钥比较必须常量时间（先 sha256 再 `hmac.compare_digest`），**不得用 `==`**。
缺 bearer key 或 worker id → 401，`status == "disabled"` → 403。小程序 token 访问任何
`/worker/v1/*` 必须 401，Worker 密钥访问任何 `/api/v1/*` 也必须 401（两个方向都有测试）。
`worker_id` 一律由服务端分配，**绝不接受客户端传入**；同一 `installation_id` 幂等返回同一个。
共享密钥与 `installation_id` 只存在于安装器写入的本地配置，不随分发包下发。

**租约与 fencing**：一个 Worker 同时只持有 1 个任务，已持有 `leased`/`running`/`uploading`
再领取返回 204。claim 在单事务内用 `with_for_update(skip_locked=True)` 按 `queued_at, id` 取 1 条。
`lease_version` 每次成功 claim **严格递增**，是防陈旧写入的 fencing token，**ACK / 续租 / 上传 / 提交
四条写路径都必须校验它**；错误 Worker 或陈旧 fence 一律 409（不是 200，也不是 500）。
续租只接受 `running`/`uploading`，过期时间**只用服务端时间**算，不接受客户端传入。

**过期策略**：60 秒无心跳标记 `suspected_offline`；**只有从未 ACK 的 `leased`** 在 30 秒后回 `queued`；
`running`/`uploading` 租约过期标记 `worker_exception`。**已开始执行的任务绝不自动重排**。
两个回收器必须在行锁下**重新校验状态**再写，否则会覆盖刚 ACK 或刚交付的任务。

**结果提交**：上传凭证单次使用，绑定 job_id / worker_id / lease_version / 类型 / 大小上限；
校验 SHA-256 与 PDF 可读性后才建 `FileObject` 行。单次使用最终由
`file_objects.relative_path` 唯一约束保证（服务层的先查后插在 MySQL 上会 race）。
重复提交幂等返回 `already_committed`，不产生第二份产物、不二次跃迁。

**所有权**：quote / payment / order 的归属一律从认证用户推导。**API 绝不接受客户端传入的 `user_id`**。
订单分页 cursor 未签名，但 owner 过滤在 SQL 层强制，伪造 cursor 读不到他人订单。

**文件**：staging 文件 + `os.replace` 原子写入；算真实 SHA-256 与字节数；防绝对路径、`..`、路径逃逸；
无效/损坏/加密/超页数/超大小 PDF 必须失败且不留 `.part`、临时 PDF 或 FileObject 行。
只用 source PDF 页数计价，reference PDF 不计价。

**支付**：前端显示"支付成功"不能创建订单，只有服务端校验过的回调可以。回调校验 quote、payment intent、
金额、归属、过期、消费状态，且必须幂等——重复与并发回调都只产生一份 Payment/Order/GradingRound/GradingJob，
幂等性同时依赖事务逻辑和 `orders.quote_session_id` 唯一约束。

**事务里不要移动或删除文件。** 支付路径上「文件状态提升」是纯数据库操作（改 `FileObject.state`），
路径不变——否则 commit 失败会让行与磁盘永久不一致。

Worker 结果提交确实要把暂存文件搬到最终路径，用的是 **copy → commit → 删暂存**：
先在事务内完成全部校验与状态跃迁并 `flush()`，再复制字节，再 commit，最后才删暂存副本。
**不要改成先 move 再 commit**——move 会销毁唯一副本，进程若在 move 之后、commit 之前崩溃，
重试将找不到源文件，任务永久卡在 `uploading`。copy 的最坏情况只是留下一个无人引用的可回收文件。

## 已锁定的目标架构决策

来自 `docs/superpowers/plans/2026-08-08-program-roadmap.md`，写新代码时按这些前提设计：

- 云服务器**不运行 Codex**；Worker 只做**出站** HTTPS 轮询，服务器从不反连用户主机。
- 一个健康 Worker 同时只持有 **1 个订单**；一个订单内部最多 3 个 Codex 会话。
- MySQL 兼任业务库和任务队列；行锁做原子 claim，`lease_version` 作为防陈旧写入的 fencing token。MVP **不引入 Redis / RabbitMQ / Docker**。
- 定价版本化，默认 ¥10/页；报价时快照金额，后续调价不影响已有订单。
- V1 交付后 3 天内可验收/ 一次复核 / 全额退款；V2 交付后 3 天内可验收 / 退款。退款成功立即撤销下载权限。
- 用户退款计入月度次数与累计金额占比；Admin 技术性退款不计入。
- 文件主存储为服务器本地磁盘，私有腾讯 COS 作加密备份。
- 三个稳定接缝要保留，便于 fake/生产适配器通过配置切换：`AuthProvider`、`PaymentGateway`、`GradingRuntime`。

远端主机（Phase 09）：SSH 别名 `grader-prod`，单机双环境（`grader-staging` → `127.0.0.1:8101`，`grader-production` → `127.0.0.1:8102`，生产初始为安装但停用）。仅 TCP 22 对外，staging 通过 SSH 端口转发访问。Nginx / certbot / 域名切换 / 真实微信支付**已被明确 gated**，等外部资质就绪。

## 已知遗留风险

- `_lock()`（`server/services/payments.py` 与 `server/services/leases.py`）在 SQLite 上退化为不加锁，
  并发正确性依赖唯一约束；MySQL 上是真正的 `SELECT ... FOR UPDATE`。
  **Phase 03 已在真实 MySQL 8.4 上验证过原子 claim**（三 Worker barrier 并发领取三个不同任务）；
  支付回调路径仍未在真实 MySQL 上验证。
- **`grading_jobs` 上的 `ix_grading_jobs_claim (state, queued_at, id)` 是原子 claim 的正确性前提，
  不是性能优化。** 缺这个索引时 MySQL 会用 filesort 解析 claim 查询，而 filesort 之后的
  `FOR UPDATE SKIP LOCKED` **会直接返回空行**，不是跳到下一条未锁定的行——实测三个并发 Worker
  只有两个能领到任务。删除或改动这个索引前务必在真实 MySQL 上复验。
- 假支付回调**无签名**，用 `prepay_id` 作查询键。换真实微信支付时必须加签名校验。
- 过期 quote、临时文件与终态订单产物已由 Phase 05 的 scheduler 清理；但 scheduler
  **需要有人真的把进程跑起来**（`python -m server.scheduler.main`），Phase 08 才有
  systemd 单元。没跑它就等于没有清理，磁盘仍会增长。
- Worker 上传凭证目前复用 `session_secret` 签名，建议后续换独立密钥。
- `workers.current_job_id` 刻意**不加数据库外键**：它与 `grading_jobs.worker_id` 会构成循环引用，
  MySQL 的建表与删表顺序都过不去。由应用层单写者（`LeaseService`）保证一致性，已有测试覆盖清除逻辑。
- 两个租约回收器（`release_unacknowledged` / `expire_started_leases`）已由 Phase 05 的
  scheduler 周期调用，仍**只通过 `LeaseService` 的方法**，不绕过 fencing。
- 退款目前走 `FakePaymentGateway`：`RefundRequest` / `RefundResult` / `RefundFailed` 接缝已就位，
  换真实微信退款时只需替换适配器。**幂等依赖支付方按 `external_refund_id` 去重**，
  接真实网关时必须确认对方确实这么做。
- `verify_backup_freshness` 是**占位**任务，返回 `skipped`；真实 COS 加密备份是 Phase 09。
- Admin 静态共享密钥认证已在 Phase 07 **移除**；`GRADER_ADMIN_SHARED_KEY` 配置项仍在
  （标注废弃，仅为兼容既有 `.env`），Phase 08 删除它与 `.env.example` 里那一行。
  会话被盗的最大伤害仍被限制为「把真实用户的全额付款退回原支付渠道」：金额与收款方不可指定。
- Admin 登录限流是**进程内**的（`app.state.admin_login_limiter`），按 username+IP 计数。
  多进程部署时每个进程各有一份计数，等效阈值会放大——Phase 08 若横向扩容需换共享存储。
- 面向用户的结果下载端点已在 Phase 06 落地，`orders.downloads_revoked_at` 现在是**活约束**：
  `server/services/result_downloads.py` 在每次下载请求里检查它，退款成功后立刻返回 410。
  已通过变异测试验证该检查是必需的（去掉它会让退款后仍可下载）。
  真机链路上的「退款后下载被拒」仍需人工验证一次，见 `miniapp/README.md` 的验收清单第 6 步。
- 小程序的真实微信登录与真实微信支付**未接通**：`miniapp/config.js` 里两条分支都存在，
  但只有 `test-` 假登录与 `simulate-success` 假支付经过实际验证。
- 支付成功后靠「轮询订单列表出现新订单」确认，因为服务端没有「按 quote 查订单」的接口；
  同账号在别处并发下单的极端情况可能误认。刻意未为此扩展服务端 API。

## 开发约定

- **计划先于代码**：`docs/superpowers/plans/` 下有 Phase 01–09 的逐步计划，含具体测试代码和 commit 信息。开工前读对应 Phase 计划，从 [计划索引](docs/superpowers/plans/README.md) 进入。
- **TDD**：先写失败测试 → 确认失败 → 实现 → 通过 → commit。每个 Task 一个 commit。
- **顺序执行，不要并行派发实现者**，以保持 Git 历史线性干净。
- 每个 Phase gate 处运行 `pytest -q` 和 `git status --short`，确认只有当前阶段的文件被改动。
- 所有表结构变更走 Alembic migration，不手工改表。
- 不提交 `.env`、密钥、学生 PDF、批改结果、日志、本地数据库。`.venv/` `tmp/` `.worktrees/` 已被 gitignore。
- README 的「当前完成度 / 仓库结构 / 已实现 API / 测试状态」四节要随阶段完成同步更新，测试数量必须来自实际运行结果。
- 本文件与 `.codebuddy/rules/` 也要随阶段同步：测试数量、Alembic head、目录状态、已实现 API 都不能留旧值。

## 环境变量

服务端（前缀 `GRADER_`，见 `.env.example`，共 11 项）：`ENVIRONMENT` / `DATABASE_URL` / `DATA_DIR` /
`SESSION_SECRET`(≥32) / `WORKER_SHARED_KEY`(≥32) / `ADMIN_SHARED_KEY`(≥32，**已废弃不再认证**) /
`ADMIN_ORIGIN` / `PRICE_CENTS_PER_PAGE` / `MAX_PDF_BYTES` / `MAX_PDF_PAGES` /
`QUOTE_TTL_SECONDS` / `ACCEPTANCE_TTL_SECONDS`（共 12 项）。
四个敏感字段都走同一套 `SecretStr` 脱敏与报错重建机制。

`ADMIN_ORIGIN` 是安全控制而非便利项：每个 Admin 写请求的 `Origin` 必须**字面相等**。

**Phase 07 起，部分运营参数改为「数据库优先、环境变量兜底」**：
`operational_settings` 表里存在的键覆盖同名配置（`MAX_PDF_PAGES` / `MAX_PDF_BYTES` /
`QUOTE_TTL_SECONDS` / `ACCEPTANCE_TTL_SECONDS` 等），由 Admin 设置页在线修改、无需重启。
表里没有该键时才回退到环境变量，所以全新部署的行为与 Phase 06 完全一致。

`ACCEPTANCE_TTL_SECONDS` 在结果交付时写入订单验收期限，Phase 05 起由售后接口与
scheduler 共同使用：窗口内可验收 / 复核 / 退款，超期由 scheduler 自动验收。

Worker 侧（前缀 `GRADER_WORKER_`）：`SERVER_BASE_URL` / `SHARED_KEY`(≥32) / `INSTALLATION_ID` /
`WORKER_ID` / `WORKSPACE_ROOT` / `DEVICE_NAME` / `WORKER_VERSION` / `POLL_WAIT_SECONDS` /
`RENEW_INTERVAL_SECONDS` / `REQUEST_TIMEOUT_SECONDS`。非本机地址强制 HTTPS。

测试可选：`GRADER_TEST_MYSQL_URL` 指向可丢弃的 MySQL 8 数据库，用于运行真实并发 claim
测试与 scheduler advisory lock 测试。

旧版的 `AI_GRADER_*` 变量已随旧项目迁出，本仓库不再使用。
