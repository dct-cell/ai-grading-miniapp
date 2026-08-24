# 数学竞赛批改服务

面向联赛二试、CMO 和 IMO 的 AI 批改服务，由微信小程序、FastAPI 服务端、管理后台和跨平台 Worker 组成。

当前代码已经覆盖上传与报价、订单和支付状态、Worker 调度、两档批改报告、验收、一次复核、退款及管理后台。真实微信登录、真实微信支付、生产部署和备份恢复尚未完成，因此目前适合本地开发和内测，不应直接作为收费服务上线。

## 评分方式

| 档位 | 输出 | 默认模型 |
|---|---|---|
| 简明评分 `summary_report` | A4 总分、分题得分、主要问题和建议 | `gpt-5.6-luna`，`max` |
| 逐页精批 `annotated_review` | 原卷逐页批注及总结页 | `gpt-5.6-sol`，`high` |

评分口径、提示词、JSON 契约、PDF 脚本和字体统一放在 `.agents/skills/olympiad-grader/`。Worker 会把该目录复制到每个隔离任务工作区，它是运行依赖，不是开发文档。

## 架构

```text
微信小程序 / Admin
        │ HTTPS
        ▼
FastAPI + MySQL + 文件存储
        │ Worker 主动轮询、续租、上传结果
        ▼
macOS / Linux / Windows Worker
Codex CLI + XeLaTeX + olympiad-grader Skill
```

- 云服务器保存用户、订单、支付、退款、任务状态和文件，不运行 Codex。
- Worker 只主动向服务端发起请求，服务端不反向连接 Worker。
- 小程序、Worker、Admin 是三个隔离的认证域。
- 价格、服务档位和评分标准在报价、订单和轮次中保存不可变快照。

## 仓库结构

```text
server/      FastAPI、数据库、订单、支付、退款、调度和管理 API
worker/      Worker 客户端、守护进程、Codex 批改运行时和环境检查
miniapp/     微信原生小程序
admin/       React/Vite 管理后台
.agents/     批改 Skill、提示词、评分规则、报告脚本和字体
tests/       Server、Worker 和集成测试
```

## 本地安装

需要 Python 3.12–3.14。运行前端测试还需要 Node.js；执行真实批改还需要 Codex CLI 和 XeLaTeX。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

创建本地 `.env`。下面是 SQLite 开发环境的最小示例；三个密钥值都应替换为至少 32 个字符的随机字符串：

```dotenv
GRADER_ENVIRONMENT=development
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/server-dev.sqlite3
GRADER_DATA_DIR=./tmp/server-data
GRADER_SESSION_SECRET=replace-with-at-least-32-random-characters
GRADER_WORKER_SHARED_KEY=replace-with-at-least-32-random-characters
GRADER_ADMIN_SHARED_KEY=deprecated-but-still-required-by-current-config
GRADER_ADMIN_ORIGIN=http://localhost:5173
GRADER_SUMMARY_PRICE_CENTS_PER_PAGE=100
GRADER_ANNOTATED_PRICE_CENTS_PER_PAGE=500
GRADER_SUMMARY_REPORT_ENABLED=true
```

`.env.example` 是 staging/MySQL 配置模板。不要提交 `.env`、数据库密码、用户 PDF、批改结果或日志。

初始化数据库并启动服务端：

```bash
mkdir -p tmp/server-data
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/server-dev.sqlite3 \
  .venv/bin/alembic upgrade head

.venv/bin/python -c 'import uvicorn; from server.config import ServerSettings; from server.main import create_app; uvicorn.run(create_app(ServerSettings()), host="127.0.0.1", port=8000)'
```

检查服务：

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

需要自动验收、租约回收、文件清理和退款重试时，另启 scheduler：

```bash
.venv/bin/python -m server.scheduler.main
```

## Worker

Worker 使用 `GRADER_WORKER_` 前缀。至少配置：

```dotenv
GRADER_WORKER_SERVER_BASE_URL=http://127.0.0.1:8000
GRADER_WORKER_SHARED_KEY=与服务端相同的至少32字符密钥
GRADER_WORKER_INSTALLATION_ID=local-worker-01
GRADER_WORKER_WORKSPACE_ROOT=./tmp/worker
GRADER_WORKER_RUNTIME_MODE=codex
```

常用命令：

```bash
.venv/bin/python -m worker.cli doctor
.venv/bin/python -m worker.cli register
.venv/bin/python -m worker.cli run-once
.venv/bin/python -m worker.cli run
.venv/bin/python -m worker.cli status
```

非本机服务地址强制使用 HTTPS。`runtime_mode=fake` 仅用于演示和测试，真实交付必须使用 `codex`。

## 前端

- 微信小程序的导入、测试和真机验收说明见 [`miniapp/README.md`](miniapp/README.md)。
- Admin 的开发服务器、账号创建和人工安全检查见 [`admin/README.md`](admin/README.md)。

## 测试

```bash
.venv/bin/python -m pytest -q
cd miniapp && npm test
cd admin && npm ci && npm test && npm run build
```

需要真实 MySQL 才能验证行锁、`SKIP LOCKED` 和 scheduler advisory lock：

```bash
GRADER_TEST_MYSQL_URL='mysql+pymysql://root@127.0.0.1:3306/grader_test' \
  .venv/bin/python -m pytest tests/integration tests/server/test_scheduler_lock.py -q
```

数据库结构只通过新增 Alembic migration 修改；当前迁移 head 为 `0007`。

## 必须保持的边界

- 服务端是价格、订单状态、可执行动作和退款金额的唯一权威，客户端不得自行决定。
- 退款金额和收款方不能来自请求；所有重试复用同一 `external_refund_id`。
- Worker 的 ACK、续租、上传和提交必须校验 `lease_version`，拒绝陈旧写入。
- 停止接单或停用 Worker 不得取消已经开始的任务。
- 假登录和假支付适配器不得在 production 注册。
- Admin 不返回服务器文件路径、`openid`、`installation_id` 或密钥。
- 调价只新增价格版本，不回写已经成交的报价或订单金额。
- 涉及订单和任务状态的修改必须使用显式状态转换规则并配套测试。

尚未完成的真实上线事项见 [`ROADMAP.md`](ROADMAP.md)。
