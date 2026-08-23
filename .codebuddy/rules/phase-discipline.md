---
alwaysApply: true
---

# 阶段纪律

本仓库按 `docs/superpowers/plans/` 里的 Phase 01–09 计划推进。开工前先读对应 Phase 计划文件，它包含逐步的失败测试、实现代码骨架和 commit 信息。索引在 `docs/superpowers/plans/README.md`。

## 事实来源

以**当前代码 + 通过的自动化测试**为唯一事实来源。计划文档描述的是未来，不是现状。

- 已落地：Phase 01（`server/` 的配置、DB、状态机、模型、迁移、健康检查）、Phase 02
  （测试账号登录、PDF 报价、假支付回调、V1 排队订单、订单列表）、Phase 03
  （Worker 认证注册、单任务原子租用、ACK/心跳/续租、过期策略、结果暂存与事务性提交、
  `worker/` 守护进程骨架）、Phase 04（`LegacyCodexRuntime` 适配器、隔离工作区、
  `worker/platforms/` 原生进程适配器、`worker/runtime/doctor.py` 八项环境自检、
  `GET /worker/v1/jobs/{id}/bundle/{kind}` 下载端点）与 Phase 05（V1/V2 验收、一次复核、
  幂等全额退款、Admin 退款审批与技术性退款、`server/scheduler/` 单例调度、跨 WorkerETA）、
  Phase 06（`miniapp/` 微信原生小程序：登录、三步创建向导、订单列表/详情与 15 秒轮询、
  结果下载、验收/复核/退款；服务端新增用户结果下载端点
  `GET /api/v1/orders/{id}/rounds/{n}/result/{kind}`）。
  Phase 07（`admin/` React/Vite 控制台：Argon2id + 不透明 Cookie 会话 + CSRF + 限流；
  总览/订单/售后/Worker/用户/资金/设置/审计七域 19 条路由；共享密钥认证已彻底移除）。
  当前 `pytest -q` 为 **788 项通过 + 6 项跳过**，外加 `miniapp/` 的 **99 项** Node 测试
  与 `admin/` 的 **64 项** Vitest（`cd admin && npm test`）+ 4 项 Playwright e2e。
  Alembic head 为 `0006`（Phase 07 新增 `admin_sessions` 与 `operational_settings`）。
- 跳过的 6 项都需要外部环境：`tests/integration/test_mysql_job_claim.py`（2 项）与
  `tests/server/test_scheduler_lock.py`（2 项）需 `GRADER_TEST_MYSQL_URL`，
  `tests/worker/test_platform_processes.py` 的 Linux / Windows 进程终止 smoke 测试
  （2 项，按宿主平台跳过）。**SQLite 静默忽略 `FOR UPDATE` 且只允许单写者，行锁正确性无法在 SQLite 上证明**。
  Phase 05 的售后/ 退款 / scheduler 测试已在一次性 MySQL 8.4 实例上跑通（88 项）。
- `worker/` 已落地真实批改运行时；`FakeGrader` 保留为演示与测试基线，
  通过 `AI_GRADER_RUNNER_MODE=demo` 切换。旧版批改实现的逐字副本在
  `worker/runtime/legacy/` 下，仅在已验证 bug 时改动。
- `miniapp/` 已在 Phase 06 落地（微信原生小程序，测试用 Node 内置 `node:test`）。
  它**只用 session token 访问 `/api/v1/*`**：不得出现 Worker 共享密钥或 Admin 密钥，
  不得调用 `/worker/v1/*` 或 `/admin/api/v1/*`（`miniapp/tests/structure.test.js` 有断言）。
  真实微信登录与真实微信支付**尚未接通**，只有假登录与假支付经过验证。
- `admin/` 已在 Phase 07 落地（React + Vite + TypeScript，**Vitest** 而非 `node:test`）。
  它**只用 Cookie 会话访问 `/admin/api/v1/*`**：不得出现任何密钥，不得调用 `/api/v1/*`
  或 `/worker/v1/*`；不得把任何令牌写进 `localStorage` / `sessionStorage`（有测试断言）。
- `ops/` 目录**尚不存在**，Phase 08–09 才创建。不要 import 它，不要在文档里写成已有功能。
- 旧版 `app/` 已于 2026-08-09 迁出到 `~/Desktop/旧的小程序`，**本仓库没有 `app` 包**，不要 import。
- 回答「XX 有没有实现」时，先 grep 或读文件确认，不要凭计划文档回答。

## 工作流

1. 先写失败测试 → 运行确认失败 → 实现 → 运行通过 → commit。每个 Task 一个 commit。
2. 阶段 gate 处运行 `.venv/bin/python -m pytest -q` 和 `git status --short`，确认只有当前阶段的文件被改动。
3. **顺序执行，不要并行派发多个实现者改同一批文件**，以保持 Git 历史线性干净。
4. 完成一个阶段后同步更新 README 的「当前完成度 / 仓库结构 / 已实现 API / 测试状态」四节，
   测试数量必须来自本轮实际运行结果，不要沿用旧数字。同时更新 `CODEBUDDY.md` 与本目录下的
   规则文件：测试数量、Alembic head、目录状态、已实现 API 都不能留旧值。

## 边界

- 业务代码进 `server/`、`worker/`、`miniapp/`、`admin/`，以及后续的 `ops/`。
- **`server/` 不得 import 已迁出的 `app` 包**；PDF 校验用 `server/adapters/pdf.py`，有测试断言。
- **`server/` 不得 import `worker/`**：Worker 是通过 HTTP 协议交互的独立进程，不是服务端的库。
  反向依赖（`worker/` 引用 `server/`）目前也仅出现在测试里，生产代码不要新增。
- 旧版批改行为和输出仍是回归基线，但它在 `~/Desktop/旧的小程序`，不随本仓库演进。
- 新增任何 fake 适配器都要纳入 `server/main.py` 的 `FAKE_ADAPTER_ENVIRONMENTS` 门禁，
  确保 production 下不注册、不进 OpenAPI、请求 404。
  **注意 `/worker/v1/*` 与 `/admin/api/v1/*` 不属于这个门禁**——Worker 控制面与 Admin
  退款审批在所有环境都注册，它们是真实端点而非假适配器。
- 表结构变更一律走 Alembic migration，不手工改表；不回写已有 migration（当前 head 为 `0004`）。
- 不提交 `.env`、密钥、学生 PDF、批改结果、日志、本地数据库。
