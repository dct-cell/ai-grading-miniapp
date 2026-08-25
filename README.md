# 数学竞赛批改服务

面向全国高中数学联赛二试、CMO 和 IMO 的数学竞赛批改小程序，由微信小程序、FastAPI 服务端、管理后台和独立 Worker 组成。

项目已支持本地完整流程和内部测试：上传答卷、报价、任务调度、实时批改进度、报告交付、验收、一次复核和退款。真实微信业务、备案、生产部署及备份恢复仍需完成上线验收，目前不应直接作为收费服务发布。

## 核心功能

- **简明评分**：生成 A4 评分报告，包含总分、分题得分、主要问题和建议。
- **逐页精批**：在原答卷上选择性标注得分依据与根本错误，并附总结页；完整流程支持排队、并行批改、阶段进度、验收和售后，评分规则与报告脚本统一位于 `.agents/skills/olympiad-grader/`。

## 架构

```text
微信小程序 / Admin
        │ HTTPS
        ▼
FastAPI + MySQL + 文件存储 + Scheduler
        │ Worker 主动轮询、续租、上传结果
        ▼
Mac mini Worker
Codex CLI + XeLaTeX + olympiad-grader Skill
```

- Server 是业务状态的唯一权威，小程序、Worker 和 Admin 使用隔离凭证。
- Worker 只主动连接 Server，并通过租约与版本令牌拒绝过期写入。

## 仓库结构

```text
server/      FastAPI、数据库、订单、支付、退款和调度
worker/      Worker 守护进程、Codex 运行时和环境检查
miniapp/     微信原生小程序
admin/       React/Vite 管理后台
.agents/     批改 Skill、评分规则和报告生成脚本
tests/       Server、Worker 和集成测试
ops/         生产部署、备份、监控和服务配置
```

## 本地快速启动

需要 Python 3.12–3.14。运行前端测试需要 Node.js；真实批改还需要 Codex CLI、有效登录和 XeLaTeX。

### 1. 安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

### 2. 配置和初始化数据库

创建 `.env`，以下为 Server 与四并发 Worker 共用的最小本地配置：

```dotenv
GRADER_ENVIRONMENT=development
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/server-dev.sqlite3
GRADER_DATA_DIR=./tmp/server-data
GRADER_SESSION_SECRET=replace-with-at-least-32-random-characters
GRADER_WORKER_SHARED_KEY=replace-with-at-least-32-random-characters
GRADER_ADMIN_SHARED_KEY=deprecated-but-still-required-by-current-config
GRADER_SUMMARY_REPORT_ENABLED=true
GRADER_WORKER_SERVER_BASE_URL=http://127.0.0.1:8000
GRADER_WORKER_INSTALLATION_ID=local-worker-01
GRADER_WORKER_WORKSPACE_ROOT=./tmp/worker-jobs
GRADER_WORKER_MAX_CONCURRENT_JOBS=4
```

```bash
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/server-dev.sqlite3 \
  .venv/bin/alembic upgrade head
```

不要提交 `.env`、密钥、数据库、用户 PDF、报告或运行日志。

### 3. 启动三个进程

```bash
# 终端 1：Server
.venv/bin/uvicorn server.entrypoint:app --host 127.0.0.1 --port 8000
# 终端 2：Scheduler
.venv/bin/python -m server.scheduler.main
# 终端 3：Worker
.venv/bin/python -m worker.cli doctor
.venv/bin/python -m worker.cli run
```

### 4. 打开前端

微信开发者工具导入 `miniapp/`；Admin 和真机调试方式见文末文档。

## 单文件本地批改

不启动 Server 时可运行 `.venv/bin/grader-local /绝对路径/答卷.pdf --standard imo --tier annotated_review`；使用 `--help` 查看其他选项。

## 测试

```bash
.venv/bin/python -m pytest -q
cd miniapp && npm test
cd admin && npm ci && npm test && npm run build
```

## 更多文档

- [微信小程序开发与真机验收](miniapp/README.md)
- [Admin 开发与账号管理](admin/README.md)
- [生产部署与运维](ops/README.md)
- [上线事项与后续计划](ROADMAP.md)
