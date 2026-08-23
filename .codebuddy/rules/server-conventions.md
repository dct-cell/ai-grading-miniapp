---
alwaysApply: false
paths: server/**/*.py, tests/server/**/*.py
---

# 服务端（`server/`）编码约定

## 状态机

任何订单 / 批改任务的状态变更必须调用 `server/domain/states.py` 的 `require_order_transition` / `require_job_transition`，不允许绕过校验直接赋值 `state`。

`ORDER_TRANSITIONS` / `JOB_TRANSITIONS` 是 `MappingProxyType` + `frozenset`，保持不可变。新增状态时同步补测试。

业务含义：一次交付为 V1，只允许一次复核（V2），`V2_DELIVERED` 不能回到 `V2_QUEUED`；任何非终态都可进入 `REFUND_PENDING`。

## 配置与密钥脱敏

`ServerSettings` 的 `database_url` / `session_secret` / `worker_shared_key` 有一套刻意设计的脱敏机制：先包成 `SecretStr`，再由 `PlainValidator` 解包校验，并用 `mode="wrap"` 校验器把 `ValidationError` 重建为不含原值的版本。

**修改这三个字段或其校验逻辑时，必须保证任何报错路径都不泄漏原值**，`tests/server/test_config.py` 有大量针对此的断言。

`production` 环境拒绝非 `mysql+pymysql://` 的数据库 URL。

新增配置项时必须同步 `.env.example`，`tests/server/test_config.py::test_env_example_documents_every_setting` 会断言两者完全一致。

## 模型约定

- 主键：UUID 字符串，`String(36)`，`default=_uuid_string`。
- 金额：整数「分」，字段名以 `_cents` 结尾。
- 时间：统一用 `server/models/base.py` 的 `UTCDateTime`。它在绑定时要求 tz-aware（否则抛错）、存库转 naive UTC、读出补回 UTC。不要直接用 `DateTime`。
- 模型按业务责任分入 `accounts` / `orders` / `payments` / `workers` / `audit`，并在 `models/__init__.py` 导出。
- 改动 schema 要同步 `tests/server/test_models.py` 的契约断言（`EXPECTED_COLUMNS`、`EXPECTED_UNIQUES`、
  `EXPECTED_NULLABLE_COLUMNS`、`EXPECTED_FOREIGN_KEYS`、`EXPECTED_DATETIME_COLUMNS`）和 `_valid_row` 数据。

## 分层

- `domain/` 是纯函数，不得 import FastAPI 或 SQLAlchemy session。
- `adapters/` 是可替换 seam（`auth` / `payments` / `files` / `pdf`）；fake 与生产实现靠配置切换。
- `services/` 承载事务性用例，`api/` 只做 HTTP 编排与错误映射。
- `scheduler/` 是独立进程入口，不由 FastAPI 装配：所有时间触发的状态跃迁只在这里发生。
  它必须通过既有 service 方法写库（如租约回收走 `LeaseService`），不得绕过 fencing 或状态机。
- `main.py` 只提供 `create_app(settings)` 工厂，没有模块级 app 单例；引擎生命周期由 `lifespan` 负责 `dispose()`。
- **不得 import 已迁出的 `app` 包**，`tests/server/test_pdf_adapter.py` 会 AST 扫描断言。
- **不得 import `worker/`**：Worker 是通过 HTTP 协议交互的独立进程，不是服务端的库。

## 环境门禁

`main.py` 的 `FAKE_ADAPTER_ENVIRONMENTS` 决定假登录、假支付、假回调是否注册。
**production 下这些路由不注册、不进 OpenAPI、请求返回 404**——新增任何 fake 适配器都要纳入它，
并补上路由表 / OpenAPI / 实际请求三种断言（见 `test_auth_environment_gate.py`、`test_fake_payment.py`）。

**`/worker/v1/*` 不属于这个门禁**：Worker 控制面用共享密钥认证，是真实端点，在所有环境都注册。

## 认证域

三个认证域必须互不相通，两个方向都有测试：

- 小程序：`Authorization: Bearer <session token>`，库里只存 sha256。
- Worker：`Authorization: Bearer <shared key>` + `X-Worker-ID`。共享密钥比较必须常量时间
  （先 sha256 再 `hmac.compare_digest`），**不得用 `==`**。
- Admin（Phase 07 起）：Argon2id 密码 + 服务端不透明会话 + HttpOnly Cookie + CSRF。
  会话 token 只存 sha256、登录后轮换；Cookie 带 `HttpOnly` / `SameSite=Strict` /
  `Path=/admin`，staging 与 production 加 `Secure`（development/test 不加）。
  **共享密钥认证已移除**，`admin_shared_key` 不再被任何认证代码读取。
  认证要求真实 `admin_users` 行这一点保留，否则 `AuditLog.actor_id` 记不到真人。
  `/admin/api/v1/*` 与 `/worker/v1/*` 一样在所有环境注册。
- **CSRF**：所有 POST/PATCH/DELETE 必须同时通过 `X-CSRF-Token` 与 `Origin` 字面匹配。
  令牌由 `session_secret` 对会话 token 哈希做 HMAC **派生**（不存列），因此多标签页稳定。
  比较**必须先 hash 再 `compare_digest`**——`compare_digest` 对非 ASCII `str` 会抛
  `TypeError`，而该值来自攻击者可控的请求头，直接比较会把 403 变成 500。
- **登录限流**：同一 username+IP 15 分钟 5 次失败 → 429，且限流期间**正确密码也拒绝**。
  `reset()` 必须只清 `(username, address)` 这一个键；按 username 清会让攻击者
  借真实管理员的每次成功登录重获配额。未知用户名要走 `_DUMMY_HASH` 支付同样的
  Argon2id 开销，否则响应时间会泄漏账号是否存在。

小程序 token 访问 `/worker/v1/*` 必须 401；Worker 密钥访问 `/api/v1/*` 也必须 401；
小程序 token 与 Worker 密钥访问 `/admin/api/v1/*` 必须 401，admin 凭据访问前两者也必须 401。
已退役的 `admin_shared_key` 访问 `/admin/api/v1/*` 也必须 401（有测试）。

## 事务与并发

- 归属一律从认证用户推导，**API 绝不接受客户端传入的 `user_id`**；`worker_id` 同理由服务端分配。
- 支付回调必须幂等：先 `flush()` 抢 `orders.quote_session_id` 唯一约束，输了就 `CallbackRejected`。
  幂等性同时依赖事务逻辑和数据库唯一约束，不能只靠其中之一。
- **支付路径上不要在数据库事务里移动或删除文件。** 文件状态提升是纯 DB 操作（改 `FileObject.state`），
  路径写入后不再变动——否则 commit 失败会让数据库行与磁盘永久不一致。
- Worker 结果提交确实要落到最终路径，用 **copy → commit → 删暂存**（见 `services/results.py`）：
  先在事务内完成全部校验与状态跃迁并 `flush()`，再复制字节，再 commit，最后删暂存。
  **不要改成先 move 再 commit**——move 销毁唯一副本，崩溃在 move 之后 commit 之前会让重试
  找不到源文件，任务永久卡在 `uploading`。
- `lease_version` 是 fencing token：ACK / 续租 / 上传 / 提交四条写路径都必须校验，
  错误 Worker 或陈旧 fence 返回 409。租约回收器必须在行锁下**重新校验状态**再写。
- `_lock()` 在 SQLite 上退化为不加锁，MySQL 上是 `SELECT ... FOR UPDATE`；写并发测试时
  注意 SQLite 只允许单写者，不要用多线程真并发去测本该由约束保证的不变量。
- **`ix_grading_jobs_claim (state, queued_at, id)` 是原子 claim 的正确性前提，不是性能优化**：
  缺索引时 MySQL 走 filesort，而 filesort 后的 `FOR UPDATE SKIP LOCKED` 会返回空行而非下一条。
- **订单状态跃迁一律用「带状态谓词的条件 UPDATE」（compare-and-set）**，不要先查后写：
  `UPDATE orders SET state=:new WHERE id=:id AND state=:observed`，再检查 `rowcount`。
  裸 `order.state = x` 会覆盖另一请求刚提交的决定——这是 Phase 05 售后三动作互斥、
  以及 scheduler 自动验收不覆盖用户退款的唯一保障。pysqlite 直到首个 DML 才 `BEGIN`，
  所以 SQLite 上先前的 SELECT 根本没锁住任何东西。
- **退款幂等**：`refunds.external_refund_id` 唯一且跨重试复用同一值；网关调用必须在事务外，
  否则无法区分「写库失败」与「钱已退出去」。只有网关成功才把订单推到 `refunded` 并写
  `downloads_revoked_at`；失败记 `refund_failed` 且订单留在 `refund_pending` 供重试。
  **一笔支付同时只允许一个存活退款行**（`pending` / `refund_failed` / `refunded`），
  且订单一旦 `refunded` 就不得再执行任何退款行——两者缺一都会导致真实的双倍退款。
- 首次创建用户 / 注册 Worker 等「查不到就插入」的路径要用 savepoint 兜住唯一约束冲突，不能 500；
  且只在确认冲突来自预期的那个唯一约束时才走恢复逻辑，其他约束错误要原样抛出。

## 迁移

Alembic 只读进程环境变量 `GRADER_DATABASE_URL`，**不加载 `.env`**（与应用运行时行为不同）：

```bash
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/server-dev.sqlite3 .venv/bin/alembic upgrade head
```

当前 head 是 `0006`（Phase 07 新增 `admin_sessions` 与 `operational_settings`）。
改 schema 时新增 migration，**不要回写 `0001`–`0006`**，
并验证空库 `upgrade head` 与「上一版 → head」两条路径得到相同 schema。

`workers.current_job_id` 刻意**不加数据库外键**：它与 `grading_jobs.worker_id` 会构成循环引用，
SQLite 与 MySQL 的建表/删表顺序都过不去。一致性由 `LeaseService` 单写者保证。
