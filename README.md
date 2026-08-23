# 数学竞赛批改服务

这是一个**微信小程序批改服务**，正在按阶段建设中。

- `server/` 是服务端，已完成 Phase 01 基础、Phase 02 的登录、报价、支付回调与订单闭环、Phase 03 的 Worker 控制面，以及 Phase 04 的 bundle 下载端点。
- `worker/` 是跨平台 Worker 守护进程，已能认证、注册、租用任务、续租、上传并提交结果，并在 Phase 04 接入了真实 Codex/XeLaTeX 批改运行时（`LegacyCodexRuntime`）和原生进程适配器（macOS / Linux / Windows）；`FakeGrader` 保留为演示与测试基线。
- `miniapp/` 是微信原生小程序，`admin/` 是 React/Vite 管理控制台（Phase 07 已落地）。
- 正式部署（systemd、Nginx、备份）仍在 Phase 08–09，尚不能把本仓库当成完整业务服务使用。

> **旧版单机批改器已于 2026-08-09 迁出本仓库**，现位于 `~/Desktop/旧的小程序`。
> 它仍是唯一验证过的批改实现和回归基线（57 项测试通过）。Phase 04 已把批改链路搬到
> 跨平台 Worker 上，`worker/runtime/legacy/` 下保留其逐字副本，仅在已验证 bug 时改动。

## 当前完成度

| 模块 | 状态 | 当前内容 |
|---|---|---|
| 服务端基础 `server/` | Phase 01 已完成 | 环境配置、数据库会话、关系模型、状态机、迁移、健康检查 |
| 上传、报价与订单 | Phase 02 已完成 | 测试账号登录、PDF 报价、假支付回调、V1 排队订单、订单列表 |
| Worker 控制面 | Phase 03 已完成 | Worker 认证与注册、单任务原子租用、ACK/心跳/续租、租约过期策略、结果暂存与事务性提交 |
| Worker 批改运行时 | Phase 04 已完成 | `LegacyCodexRuntime` 适配器、隔离工作区、进程适配器、环境 doctor、bundle 下载端点 |
| 复核、退款与文件生命周期 | Phase 05 已完成 | V1/V2 验收窗口、一次复核、幂等全额退款、Admin 审批与技术性退款、scheduler 时间触发、跨 Worker ETA |
| 微信小程序 `miniapp/` | Phase 06 已完成 | 登录与首页、三步创建向导、订单列表/详情与轮询、结果下载、验收/复核/退款；服务端新增用户结果下载端点。真实微信登录与支付未接通，真机验收待人工执行 |
| 管理后台 `admin/` | Phase 07 已完成 | Argon2id 密码 + 服务端不透明会话 + HttpOnly Cookie + CSRF；总览、订单、售后、Worker 管控、用户、资金、设置、审计九条路由；每个写操作产生审计事件 |
| 部署配置 `ops/` | 未创建 | 计划在 Phase 08–09 建设 |

完整阶段说明见[实施路线图](docs/superpowers/plans/2026-08-08-program-roadmap.md)。

## 目标架构

最终系统由三类运行节点组成：

```text
微信小程序 / Admin
        │ HTTPS
        ▼
大陆云服务器
FastAPI + MySQL + 本地文件 + COS 备份
        │ Worker 主动轮询、续租和上传结果
        ▼
macOS / Linux / Windows Worker
Codex CLI + XeLaTeX + 批改运行时
```

核心边界如下：

- 云服务器保存用户、订单、支付、退款、任务状态和文件，不在服务器上运行 Codex。
- Worker 只主动向服务器发起 HTTPS 请求，不要求服务器反向连接用户主机。
- 每个健康 Worker 同时领取一个订单；一个订单内部最多可以使用三个 Codex 会话。
- MySQL 在 MVP 中同时承担业务数据库和任务队列职责，暂不引入 Redis、RabbitMQ 或 Docker。
- 业务文件以服务器本地磁盘为主存储，私有腾讯 COS 作为备份。

这些是已经确认的目标设计；只有“当前完成度”中标为已完成的部分已经落到代码中。

## 仓库结构

```text
math-competition-grader/
├── server/                      服务端；当前完成 Phase 01–05
│   ├── config.py                环境配置与敏感字段保护
│   ├── db.py                    SQLAlchemy Engine 和 Session 工厂
│   ├── domain/states.py         订单、批改任务状态机
│   ├── domain/refund_policy.py  用户退款自动/人工路由的纯函数
│   ├── domain/eta.py            跨 Worker 完成时间估算（最小堆模拟）
│   ├── models/                  用户、订单、支付、Worker、审计模型
│   ├── adapters/                认证、PDF 校验、文件存储、支付（含退款）的可替换实现
│   ├── services/                会话、文件、报价、支付、订单、Worker、租约、结果、售后、退款用例
│   ├── schemas/                 请求与响应模型
│   ├── api/                     小程序路由、支付回调、Worker 控制面、Admin 退款路由
│   ├── scheduler/               单例调度进程：验收超时、租约回收、文件清理、退款对账
│   ├── migrations/              Alembic 环境与数据库迁移
│   └── main.py                  FastAPI 工厂、健康检查与路由装配
├── worker/                      跨平台 Worker 守护进程；Phase 03 控制面 + Phase 04 批改运行时
│   ├── config.py                本地配置（共享密钥与installation_id 不入分发包）
│   ├── client.py                出站 HTTPS 协议客户端（含 bundle 下载）
│   ├── cli.py                   register / doctor / run / run-once / status / drain
│   ├── platforms/               macOS / Linux / Windows 原生进程适配器
│   ├── runtime/                 守护循环、租约续期、FakeGrader、LegacyCodexRuntime、workspace、doctor
│   └── assets/                  doctor 的金标准 PDF 与预期 JSON
├── miniapp/                     微信原生小程序（Phase 06）
│   ├── app.js app.json          入口、路由、tabBar
│   ├── config.js                环境档案：staging 假登录/假支付，production 真实微信路径
│   ├── services/                API 客户端、会话、登录、报价、支付、订单、下载、售后
│   ├── utils/                   格式化、订单状态词表、multipart 编码
│   ├── components/              pdf-picker、price-summary、order-card、status-pill
│   ├── pages/                   home、account、create、orders、aftersales
│   └── tests/                   Node 内置 node:test 测试（无第三方框架）
├── tests/server/                服务端测试
├── tests/worker/                Worker 守护进程与运行时测试
├── tests/integration/           跨组件测试（含需真实 MySQL 的并发测试）
├── docs/superpowers/plans/      总路线和 Phase 01–09 实施计划
├── .agents/skills/              批改 Skill、评分口径、排版脚本和字体资源
├── pyproject.toml               项目元数据和完整依赖
└── alembic.ini                  数据库迁移配置
```

`ops/` 是目标目录，目前尚不存在，Phase 08–09 会按计划创建。
`admin/` 已在 Phase 07 落地，见 [admin/README.md](admin/README.md)（含手动验收清单）。
`miniapp/` 已在 Phase 06 落地，见 [miniapp/README.md](miniapp/README.md)（含手动真机验收清单）。

`.agents/skills/olympiad-grader/` 保留在本仓库，Phase 04 已通过 `worker/runtime/workspace.py`
把它复制进每个 Worker 任务目录；`~/Desktop/旧的小程序` 里有一份相同内容供旧版独立运行，
调整评分口径时两边都要改。

## 开发环境

### 基础要求

- Python `3.12`–`3.14`
- Git
- 开发或测试服务端时可使用 SQLite
- staging/production 使用 MySQL 8；production 配置会拒绝非 MySQL URL

运行真实批改任务需要 Codex CLI 和 XeLaTeX，但那属于已迁出的旧版和未来的 Worker，
本仓库当前的测试都不需要它们。

除非某一段另有说明，下面所有命令都应在仓库根目录 `math-competition-grader/` 执行。

### 安装完整开发依赖

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

`pyproject.toml` 是唯一的依赖入口。旧版的 `requirements.txt` 已随旧项目迁出。

## 新服务端：Phase 01–05

### 本地配置

`.env.example` 是 staging/MySQL 示例。若只在本机验证新服务端，可以新建 `.env`：

```dotenv
GRADER_ENVIRONMENT=development
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/server-dev.sqlite3
GRADER_DATA_DIR=./tmp/server-data
GRADER_SESSION_SECRET=replace-with-at-least-32-random-characters
GRADER_WORKER_SHARED_KEY=replace-with-at-least-32-random-characters
GRADER_PRICE_CENTS_PER_PAGE=500
GRADER_SUMMARY_PRICE_CENTS_PER_PAGE=100
GRADER_ANNOTATED_PRICE_CENTS_PER_PAGE=500
```

`ServerSettings` 会在进程启动时自动读取仓库根目录的 `.env`，不需要手工 `source`。
`.env` 和 `tmp/` 均已被 Git 忽略。不要提交数据库密码、Session Secret 或 Worker Shared Key。

主要配置项：

| 环境变量 | 含义 | 默认约束 |
|---|---|---|
| `GRADER_ENVIRONMENT` | `development/test/staging/production` | production 必须使用 MySQL |
| `GRADER_DATABASE_URL` | SQLAlchemy 数据库 URL | 必填、错误输出会脱敏 |
| `GRADER_DATA_DIR` | 服务端数据目录 | 必填，readiness 会验证可写性 |
| `GRADER_SESSION_SECRET` | 小程序会话密钥 | 至少 32 个字符 |
| `GRADER_WORKER_SHARED_KEY` | Worker 共享认证密钥 | 至少 32 个字符 |
| `GRADER_ADMIN_SHARED_KEY` | Admin 共享认证密钥 | 至少 32 个字符；配合 `X-Admin-ID` 使用 |
| `GRADER_PRICE_CENTS_PER_PAGE` | 逐页精批兼容价格，单位为分 | 默认 `500`，即 5 元/答卷页 |
| `GRADER_SUMMARY_PRICE_CENTS_PER_PAGE` | 简明评分价格，单位为分 | 默认 `100`，即 1 元/答卷页 |
| `GRADER_ANNOTATED_PRICE_CENTS_PER_PAGE` | 逐页精批价格，单位为分 | 默认 `500`，即 5 元/答卷页 |
| `GRADER_MAX_PDF_BYTES` | 单份 PDF 字节上限 | 默认 25 MB |
| `GRADER_MAX_PDF_PAGES` | 单份 PDF 页数上限 | 默认 30 页 |
| `GRADER_QUOTE_TTL_SECONDS` | 报价与临时文件有效期 | 默认 `86400`，即 24 小时 |
| `GRADER_ACCEPTANCE_TTL_SECONDS` | 交付后验收期限 | 默认 `259200`，即 3 天 |

`GRADER_ACCEPTANCE_TTL_SECONDS` 在结果交付时写入订单验收期限，Phase 05 起由售后接口
和 scheduler 共同使用：窗口内可验收 / 复核 / 退款，超期由 scheduler 自动验收。

### 数据库迁移

Alembic 只从进程环境读取 `GRADER_DATABASE_URL`，不会自动加载 `.env`。本地 SQLite 示例：

```bash
mkdir -p tmp/server-data
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/server-dev.sqlite3 \
  .venv/bin/alembic upgrade head
```

`create_app` 也会自动创建 `GRADER_DATA_DIR`，上面的显式建目录让迁移和首次启动使用同一套本地路径。

staging/production 应把同一个变量指向对应的 MySQL 数据库后再执行迁移。

### 启动当前服务端

新服务端目前只有应用工厂，还没有正式 CLI 或 systemd 入口。下面的命令会通过
`ServerSettings` 自动加载 `.env`：

```bash
.venv/bin/python -c 'import uvicorn; from server.config import ServerSettings; from server.main import create_app; uvicorn.run(create_app(ServerSettings()), host="127.0.0.1", port=8000)'
```

健康检查：

- `GET /health/live`：进程存活检查。
- `GET /health/ready`：验证数据库连接和数据目录可写性。

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

### 已实现的 Phase 02 接口

小程序接口都要求 `Authorization: Bearer <access_token>`，归属一律从会话推导，
不接受客户端传入的 `user_id`。

| 方法与路径 | 作用 |
|---|---|
| `POST /api/v1/auth/login` | 用测试账号 code 换取会话；原始 token 只返回一次 |
| `GET /api/v1/me` | 读取当前登录用户 |
| `POST /api/v1/quotes` | 上传答卷 PDF 和可选参考 PDF，按页数报价 |
| `GET /api/v1/quotes/{quote_id}` | 读取自己的报价 |
| `POST /api/v1/payments/prepay` | 为自己的报价创建预支付意图 |
| `GET /api/v1/orders` | 按`all`/`grading`/`acceptance` 分类翻页读取自己的订单 |
| `GET /api/v1/orders/{order_id}` | 读取自己的订单详情、批改轮次、`available_actions` 与 `eta` |

支付相关的关键约定：

- 只有服务端校验过的支付回调可以创建正式订单，前端显示“支付成功”不会创建订单。
- 回调会校验报价归属、金额、过期时间和消费状态，并且是幂等的：重复回调和并发回调
  都只会产生一份 Payment、Order、GradingRound 和 GradingJob。
- 支付成功后订单进入 `v1_queued`，同时创建第 1 轮批改和 `queued` 状态的批改任务。
- 报价与上传的临时文件默认 24 小时过期；只有答卷 PDF 参与计价，参考 PDF 不计价。

下面两个假支付入口只在 `development`、`test` 和 `staging` 注册，`production`
配置下不会出现在路由表和 OpenAPI 文档中，请求返回 404：

| 方法与路径 | 作用 |
|---|---|
| `POST /api/v1/payments/{payment_id}/simulate-success` | 由报价所有者触发同一套已校验回调 |
| `POST /callbacks/fake/pay` | 假支付网关的服务端回调 |

Admin API 目前只有Phase 05 的退款审批（见下文），完整 Admin 控制台是 Phase 07；
真实微信登录与微信支付要等外部资质就绪。

### 已实现的 Phase 03 Worker 接口

Worker 控制面是**独立于小程序的第三个认证域**，与小程序会话、Admin Cookie 互不相通：
用共享密钥 `GRADER_WORKER_SHARED_KEY`（`Authorization: Bearer`）加上独立的 `X-Worker-ID`。
小程序 token 访问任何 `/worker/v1/*` 返回 401；Worker 密钥访问任何 `/api/v1/*` 也返回 401。
这些路由在**所有环境**（含 `production`）都注册，它们不是假适配器。

| 方法与路径 | 作用 |
|---|---|
| `POST /worker/v1/register` | 用共享密钥注册；同一 `installation_id` 始终返回同一 `worker_id` |
| `POST /worker/v1/heartbeat` | 上报存活与阶段，可顺带续租以减少请求数 |
| `POST /worker/v1/jobs/lease` | 领取一个排队任务；最长长轮询 25 秒，`Prefer: wait=0` 立即返回 |
| `POST /worker/v1/jobs/{job_id}/ack` | 确认已开始执行，`leased` → `running` |
| `POST /worker/v1/jobs/{job_id}/renew` | 用服务端时间续租，只接受 `running` / `uploading` |
| `POST /worker/v1/jobs/{job_id}/result/uploads` | 换取单次使用的上传凭证，`running` → `uploading` |
| `PUT /worker/v1/jobs/{job_id}/result/{kind}` | 上传 `result_json` / `result_pdf`到按租约隔离的暂存区 |
| `POST /worker/v1/jobs/{job_id}/result/commit` | 事务性交付结果；重复提交幂等返回 `already_committed` |
| `GET /worker/v1/jobs/{job_id}/bundle/{kind}` | Phase 04 新增：按租约绑定的下载令牌取 `source` / `reference` PDF |

Worker 协议的关键约定：

- **一个健康 Worker 同时只持有一个任务**；已持有 `leased`/`running`/`uploading` 的 Worker
  再次领取会得到 204。
- 领取在一个事务内用行锁完成（MySQL 上是 `SELECT ... FOR UPDATE SKIP LOCKED`），
  按 `queued_at, id` 取最早的一条。
- `lease_version` 是**防陈旧写入的 fencing token**：每次成功领取都严格递增，
  ACK、续租、上传和提交四条写路径都会校验它；陈旧或他人的租约一律返回 409。
- 过期策略：60 秒无心跳把 Worker 标记 `suspected_offline`；**只有从未 ACK 的 `leased`**
  任务会在 30 秒后退回 `queued`；`running`/`uploading` 租约过期标记 `worker_exception`。
  **已经开始执行的任务绝不会被自动重排**，避免重复批改。
- 上传凭证单次使用，且绑定 `job_id`、`worker_id`、`lease_version`、文件类型和大小上限；
  服务端校验 SHA-256 与 PDF 可读性之后才登记文件。
- 提交先在事务内完成全部校验与状态跃迁，再把暂存文件**复制**到最终路径，然后提交，
  最后才删除暂存副本。这样即使进程在中途崩溃，暂存副本仍然完好、重试仍可成功，
  最坏情况只是留下一个无人引用的可回收文件。
- 交付成功后订单进入 `v1_delivered` / `v2_delivered`，并设置 3 天验收期限。
- Phase 04 的 bundle 下载端点把下载令牌写入 `grading_jobs.bundle_download_tokens`
  （JSON 列），令牌随每次领取重新生成；租约被回收后旧令牌立即失效，旧 Worker 无法继续读取学生数据。

Worker 只做**出站** HTTPS 请求，服务器从不反向连接用户主机。

### Worker 守护进程

`worker/` 在 Phase 04 已接入真实批改运行时：`LegacyCodexRuntime` 把已验证的
`codex_runner` 包裹成 `GradingRuntime` 协议，`worker/platforms/` 提供 macOS / Linux /
Windows 原生进程组与终止逻辑，`worker/runtime/workspace.py` 把下载的 PDF 复制成旧
runner 期望的 `input/` 布局并硬链接字体文件。`FakeGrader` 保留为演示与回归基线，
通过 `AI_GRADER_RUNNER_MODE=demo` 切换。

```bash
.venv/bin/python -m worker.cli doctor      # 八项环境自检；任一失败返回 1
.venv/bin/python -m worker.cli register    # 注册并取得 worker_id
.venv/bin/python -m worker.cli run-once    # 领取并处理一个任务
.venv/bin/python -m worker.cli run         # 持续轮询
.venv/bin/python -m worker.cli status      # 查看当前持有的任务
.venv/bin/python -m worker.cli drain       # 处理完当前任务后停止领取
```

Worker 侧配置使用 `GRADER_WORKER_` 前缀（`SERVER_BASE_URL` / `SHARED_KEY` /
`INSTALLATION_ID` / `WORKER_ID` / `WORKSPACE_ROOT` 等）。共享密钥与 `installation_id`
只存在于安装器写入的本地受保护配置里，**不随分发包下发**；`doctor` 与日志都不会打印它们。
非本机地址强制要求 HTTPS。ZIP 打包与安装器属于 Phase 08范围，本阶段未实现。

### 已实现的 Phase 05 售后接口

V1 交付后 3 天内可以验收、发起一次复核或申请全额退款；V2 交付后只能验收或退款
（**没有第三轮**，V2 复核请求返回 409）。三个动作互斥：每个动作都在自己的事务里用
「带状态谓词的条件 UPDATE」抢占状态跃迁，所以并发提交时恰好一个成功，另一个 409，
Appeal 与 Refund 不会同时存在。

| 方法与路径 | 作用 |
|---|---|
| `POST /api/v1/orders/{order_id}/accept` | 验收订单，进入终态 `accepted` |
| `POST /api/v1/orders/{order_id}/review` | 买一次复核：建第 2 轮与 `queued` 任务，订单转 `v2_queued` |
| `POST /api/v1/orders/{order_id}/refund` | 申请全额退款；符合策略时立即执行，否则等待 Admin 审批 |
| `GET /api/v1/orders/{order_id}/rounds/{n}/result/{kind}` | Phase 06：下载 `result_pdf` / `result_json`；退款后返回 410 |

结果下载端点**在所有环境注册**（交付结果是真实功能，不是假适配器），且**不使用短期令牌**：
令牌是被缓存的授权决定，而 `orders.downloads_revoked_at` 恰恰不能被缓存——退款后必须立刻失效。
文件由本应用从本地磁盘直接流式返回，所以每次请求都重新校验会话与三项条件：
归属（SQL 层 JOIN 强制，非归属返回 404 而非 403）、未撤销下载（否则 410，在定位文件之前检查）、
轮次已交付且有对应产物（否则 404）。`wx.downloadFile` 支持请求头，前端用同一个 session token 鉴权。

订单详情新增三个字段：

- `available_actions`：服务端权威的可执行动作列表（`accept` / `review` / `refund`）。
  小程序据此渲染按钮，但它只是提示：每个动作仍会在自己的事务里重新校验一次。
- `appeal_text`：用户提交的复核理由。
- `eta`：跨Worker 估算的完成区间（`earliest_minutes` / `latest_minutes` 与绝对时间）。
  没有就绪 Worker、订单无待办任务、或任务已 `worker_exception` 时为 `null`。

退款的关键约定：

- **金额永不来自请求**：一律取订单已付金额，退回原支付交易。用户端和 Admin 端都是如此。
- **复核复用同一份不可变文件**：第 2 轮批改的是同一份答卷与参考 PDF，不接受替换上传。
- **退款路由**（`server/domain/refund_policy.py`，用 `Decimal` 精确计算）：

  | 条件 | 结果 |
  |---|---|
  | 金额 ≤ 5000 分 **且**（本月退款次数 < 4 **或** 预计累计占比 ≤ 30%） | `automatic`，立即执行 |
  | 其余情况 | `manual`，等待 Admin 审批 |

  金额是必要条件；次数与占比满足其一即可。次数按**Asia/Shanghai 日历月**统计，
  且只计已成立的用户退款——被 Admin 驳回或网关失败的申请不占用配额。
- **幂等**：`refunds.external_refund_id` 唯一且**跨重试复用同一个值**，支付方据此去重。
  网关调用在事务外进行；只有成功才会把订单推到 `refunded` 并写 `downloads_revoked_at`，
  失败记为 `refund_failed` 并保留 `refund_pending` 供重试。
- **一笔支付只会退一次**：同一支付上已存在 `pending` / `refund_failed` / `refunded` 的退款时，
  技术性退款会复用它而不是新建；订单一旦到达 `refunded`，其它退款行不再执行。

### 已实现的 Phase 07 Admin 接口

Admin 是**第三个独立认证域**。Phase 05 的静态共享密钥接缝已在 Phase 07
**彻底移除**：`server/` 里不再有任何代码用 `GRADER_ADMIN_SHARED_KEY` 做认证
（配置项本身保留但标注废弃，以便既有部署的 `.env` 仍能加载，Phase 08 删除）。

现在的认证是 **Argon2id 密码 + 服务端不透明会话 + HttpOnly Cookie + CSRF 令牌**：

- 会话 token 32 字节随机，库里只存 sha256；登录成功轮换（旧会话立即撤销）。
- Cookie 带 `HttpOnly` / `SameSite=Strict` / `Path=/admin`；staging 与 production 额外加
  `Secure`（development 与 test 不加，否则浏览器会拒收 http 下的 Secure Cookie）。
- 会话是**不透明**而非签名令牌：「该管理员是否仍启用」这个判断不能被缓存，
  停用账号必须在**下一次请求**立即生效。
- CSRF 令牌由 `session_secret` 对会话的 token 哈希做 HMAC **派生**而非存储，
  因此多标签页取到同一个值、不会互相踢掉；所有 POST/PATCH/DELETE 必须同时通过
  `X-CSRF-Token` 常量时间校验与 `Origin` 字面匹配（`GRADER_ADMIN_ORIGIN`）。
- 同一 username+IP 15 分钟内失败 5 次返回 **429**，且被限流时**即使密码正确也拒绝**；
  未知用户名与密码错误返回完全相同的响应，并同样支付一次 Argon2id 校验开销，
  因此响应体与响应时间都不泄漏账号是否存在。

这些路由和 `/worker/v1/*` 一样在**所有环境（含 production）都注册**——退款审批必须能在
生产环境工作，它们不是假适配器。认证仍要求一行存活的 `admin_users` 记录，
所以 `AuditLog.actor_id` 记录的是真人，Phase 05 写下的审计记录依然有效。

| 方法与路径 | 作用 |
|---|---|
| `POST /admin/api/v1/auth/login` | 用户名 + 密码登录，成功后只通过 Set-Cookie 下发会话 |
| `GET /admin/api/v1/auth/session` | 当前管理员与 CSRF 令牌 |
| `POST /admin/api/v1/auth/logout` | 撤销当前会话 |
| `GET /admin/api/v1/overview` | 总览指标，**取自同一次一致性快照** |
| `GET /admin/api/v1/orders` | 跨用户订单搜索（keyset 分页，**无归属过滤**） |
| `GET /admin/api/v1/orders/{id}` | 订单详情：轮次、任务、附件（**只有逻辑名与大小**）、时间线 |
| `GET /admin/api/v1/aftersales` | 退款审核队列 |
| `POST /admin/api/v1/refunds/{id}/approve` | 批准人工退款，复用 `RefundService` 同一套幂等执行 |
| `POST /admin/api/v1/refunds/{id}/reject` | 驳回：订单回 `accepted`，**保留下载权** |
| `POST /admin/api/v1/refunds/technical` | 技术性退款：绕过用户策略，不计入用户指标 |
| `GET /admin/api/v1/workers` | Worker 列表（**不含 `installation_id`**，它是注册凭证的一半） |
| `POST /admin/api/v1/workers/{id}/drain` | 停止派发新任务，**不取消正在执行的任务** |
| `POST /admin/api/v1/workers/{id}/disable` | 硬停；同样**不取消**在途任务 |
| `POST /admin/api/v1/workers/{id}/enable` | 恢复接单 |
| `GET /admin/api/v1/users/{public_id}` | 用户指标（**不返回 `openid`**） |
| `GET /admin/api/v1/funds` | 收款与退款汇总；**不声称银行已结算** |
| `GET /admin/api/v1/settings` | 运营参数；**永不返回任何密钥** |
| `PATCH /admin/api/v1/settings` | 修改运营参数（allow-list + 逐项范围校验） |
| `POST /admin/api/v1/settings/price-rules` | 调价：**新建版本**，已有报价金额不变 |
| `GET /admin/api/v1/audit` | 审计视图，**只增不改不删**（没有 update/delete 路由） |

即使会话被盗，可造成的资金伤害仍被限制为「把某个真实用户的全额付款退回原支付渠道」：
金额与收款方都不可指定，请求里多传 `amount_cents` 之类的字段会被 422 拒绝。
审批在扣款前后各写一条审计，因此中途崩溃也留有授权者记录。
**每个写操作都产生 `AuditLog`**（已逐条核验全部八个写路由）。

前端在 `admin/`（React + Vite + TypeScript），**不持有任何凭据**：
会话完全依赖 HttpOnly Cookie，`localStorage` 与 `sessionStorage` 始终为空（有测试断言）。
手动验收清单见 [admin/README.md](admin/README.md)。

### Phase 05 调度进程

时间触发的状态跃迁由**单个** scheduler 进程独占，通过 MySQL 具名 advisory lock
`grader-scheduler` 保证。SQLite 上没有对应原语，锁会优雅退化为空操作并报告
`enforced = False`——本地单进程可用，但不会假装在保护生产环境。

```bash
.venv/bin/python -m server.scheduler.main   # 取到锁才开始循环，默认每 20 秒一轮
```

七个任务，每个都选取有限批次、在观察到的状态上加谓词、可重复运行：

| 任务 | 作用 |
|---|---|
| `release_unacknowledged_leases` | 从未 ACK 的租约 30 秒后回 `queued`（经 `LeaseService`，不绕过 fencing） |
| `mark_expired_running_leases` | `running`/`uploading` 租约过期标 `worker_exception`，**绝不重排** |
| `auto_accept_expired_orders` | 验收窗口过期的已交付订单自动 `accepted` |
| `delete_expired_quotes` | 清理未支付、已过期报价的PDF（已支付的报价文件不动） |
| `delete_expired_order_files` | 清理终态订单超出保留期的产物 |
| `retry_failed_refund_queries` | 重试 `refund_failed` 的退款，复用原`external_refund_id` |
| `verify_backup_freshness` | **占位**：真实COS 加密备份是 Phase 09，当前返回 `skipped` |

自动验收会输给并发的用户退款：条件 UPDATE 的状态谓词保证用户决定不被覆盖。
文件清理先删字节再把行标记为 `deleted`——与结果提交的 copy → commit → 删暂存**顺序相反**，
因为那里字节是唯一副本必须能回滚，而这里的意图就是销毁。

## 旧版本地批改器
旧版已迁出到 `~/Desktop/旧的小程序`，那里有独立的 README、依赖清单和 macOS 启动器。
它保持57 项测试通过，是验证真实批改效果与评分口径的入口。

本仓库不再包含旧版代码，也不再用`requirements.txt` 或 `AI_GRADER_*` 环境变量。
Phase 04 会以它为参照，把 Codex/XeLaTeX 批改链路搬到跨平台 Worker 上。

## 业务流程

以下流程已经确定；第 1–7 步已经以测试账号、假支付和真实批改运行时的形式打通，
第 8 步（完整 Admin 控制台）尚未实现：

1. 用户通过微信登录。（当前是测试账号登录，真实微信登录待资质就绪。）
2. 上传包含题目和作答的 PDF，可选上传标准答案或评分标准。
3. 系统读取页数并按版本化价格报价，当前默认人民币 10 元/页。
4. 支付成功回调后创建正式订单并进入批改队列。（当前是假支付网关的服务端回调。）
5. Worker 领取任务，使用已验证的批改引擎生成结果并上传服务器。（Phase 04 已接入
   `LegacyCodexRuntime`；`FakeGrader` 保留为演示与回归基线。）
6. 用户可验收、申请一次原卷复核，或在规则允许时申请全额退款。（Phase 05 已实现；
   符合策略的退款立即执行，超限的等待 Admin 审批。）
7. 结果文件在终态订单后保留 30 天由 scheduler 清理；退款成功立即撤销下载权限
   （写 `downloads_revoked_at`；面向用户的下载端点本身是 Phase 06）。
8. Admin 用于处理异常订单、人工退款、Worker 状态、配置和审计记录。（Phase 05 只实现了
   退款审批的最小接缝，完整控制台是 Phase 07。）

业务规则的权威描述仍以[实施路线图和阶段计划](docs/superpowers/plans/README.md)为准。

## 测试

运行全部测试：

```bash
.venv/bin/python -m pytest -q
```

只验证服务端与 Worker：

```bash
.venv/bin/python -m pytest tests/server -q
.venv/bin/python -m pytest tests/worker -q
```

自动化测试使用临时目录和 SQLite，不要求本机安装 MySQL，也不会发起真实 Codex 调用，
因此不产生费用。

截至 Phase 07 完成时的实际验证结果：

- `tests/server/`：651 项通过，2 项跳过（scheduler advisory lock 的 MySQL 测试）。
- `tests/worker/`：134 项通过，2 项跳过（Linux / Windows 进程终止 smoke 测试）。
- `tests/integration/`：3 项通过，2 项跳过（需真实 MySQL 8 的并发领取测试）。
- 完整测试集：**788 项通过，6 项跳过**（旧版 57 项已随旧项目迁出，在 `~/Desktop/旧的小程序` 独立运行）。
- 小程序前端：**99 项通过**（`cd miniapp && npm test`，Node 内置 `node:test`，不需要微信开发者工具）。
- Admin 前端：**64 项通过**（`cd admin && npm test`，Vitest + Testing Library），外加 **4 项 Playwright**
  端到端测试在真实 Chromium 中通过（验证 HttpOnly / SameSite=Strict / Path=/admin Cookie 真的可用）。
- 数据库迁移 head：`0006`——Phase 07 新增 `admin_sessions`（0005）与 `operational_settings`（0006）。

被跳过的 6 项都需要外部环境：4 项需要真实 MySQL 8（2 项原子领取 + 2 项 scheduler
advisory lock），2 项按宿主平台跳过。**SQLite 会静默忽略 `FOR UPDATE` 且只允许单写者，
所以行锁的正确性无法在 SQLite 上证明**，其余测试改用确定性的交错、服务层直调和约束断言来
固定并发不变量，不靠 sleep 掩盖竞态。

Phase 05 的售后、退款与scheduler 测试**已在一次性MySQL 8.4 实例上验证过**（88 项通过），
包括 advisory lock 真的会拒绝第二个 scheduler、进程崩溃后锁会释放，以及退款幂等与
条件 UPDATE 在真实行锁下成立。

要在本机跑 MySQL 相关测试，需要一个可丢弃的 MySQL 8 数据库：

```bash
GRADER_TEST_MYSQL_URL="mysql+pymysql://root@127.0.0.1:3306/grader_test" \
  .venv/bin/python -m pytest tests/integration tests/server/test_scheduler_lock.py -q
```

## 实施路线

开发按依赖关系依次推进：

1. Phase 01：服务端基础——已完成。
2. Phase 02：上传、报价和订单——已完成。
3. Phase 03：多 Worker 注册、认证、租约和任务调度——已完成。
4. Phase 04：跨平台 Worker 与 Codex/XeLaTeX 批改运行时——已完成。
5. Phase 05：验收、复核、退款和文件生命周期——已完成。
6. Phase 06：原生微信小程序。
7. Phase 07：独立 Admin（会用 Argon2id + Cookie 会话替换 Phase 05 的最小 admin 接缝）。
8. Phase 08：部署、备份、恢复和切换。
9. Phase 09：远端 Ubuntu 服务器环境。

从[计划索引](docs/superpowers/plans/README.md)进入各阶段文档。域名、微信支付和小程序正式资质未就绪时，仍可以用假登录、假支付和本地/隧道环境完成大部分后端与 Worker 集成测试；真实支付回调、正式小程序发布和域名切换必须等待外部条件具备。

## 开发约定

- 以当前代码和自动化测试为事实来源，不把计划中的目录或接口写成已实现功能。
- 业务代码进入 `server/`、`worker/`、后续 `miniapp/`、`admin/` 和 `ops/`。
- `server/` 不得import 已迁出的 `app` 包；PDF 校验用 `server/adapters/pdf.py`。
  `tests/server/test_pdf_adapter.py` 会断言这一点。
- 旧版批改行为和输出仍是回归基线，但它现在位于 `~/Desktop/旧的小程序`，不随本仓库演进。
- 所有数据库结构变更必须通过 Alembic migration，不手工修改生产表。
- 不提交 `.env`、密钥、用户 PDF、批改结果、日志或本地数据库。
- staging 与 production 使用不同的数据库、数据目录、密钥和支付配置。
- 涉及订单状态和批改任务状态的修改必须通过 `server/domain/states.py` 中的显式转换规则。

## 文档入口

- [实施计划索引](docs/superpowers/plans/README.md)
- [总体实施路线图](docs/superpowers/plans/2026-08-08-program-roadmap.md)
- [Phase 01 服务端基础计划](docs/superpowers/plans/2026-08-08-phase-01-foundation.md)
- [Phase 02 上传与订单计划](docs/superpowers/plans/2026-08-08-phase-02-intake-order.md)
- [Phase 03 Worker 控制面计划](docs/superpowers/plans/2026-08-08-phase-03-worker-control-plane.md)
- [仓库整理计划](docs/superpowers/plans/2026-08-09-repository-cleanup.md)

后续每完成一个阶段，都应同步更新本 README 的“当前完成度”“仓库结构”“启动方式”和“测试状态”，避免计划状态与真实代码再次脱节。
