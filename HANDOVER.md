# 项目交接说明

面向**接手本项目的开发者**。读完这份文档你应该能：在**没有任何域名、没有微信资质、没有云服务器**的情况下，
在自己的机器上把三个组件全部跑起来并做完整的端到端测试。

> 本文档写于 Phase 07 合并后（`main` = `98dd272`）。下面每条命令与每个数字都在
> macOS + Python 3.14 + Node 26 上实际执行过，不是照抄计划文档。

---

## 一、这是什么

数学竞赛（联赛二试 / CMO / IMO）解答 PDF 的 **AI 批改服务**。用户在微信小程序上传答卷 PDF、
按页付费，服务端把批改任务分发给分布式 Worker，Worker 在**本地**调用 Codex + XeLaTeX
生成带批注的 PDF 回传，用户下载结果，并可申请复核或退款。

四个组件：

| 目录 | 是什么 | 跑在哪 |
|---|---|---|
| `server/` | FastAPI 服务端 + MySQL（开发用 SQLite） | 云服务器（尚未部署） |
| `worker/` | 跨平台 Worker 守护进程，真正执行批改 | 开发者/运营者的个人电脑 |
| `miniapp/` | 微信原生小程序（用户端） | 微信客户端 |
| `admin/` | React + Vite 管理控制台（运营端） | 浏览器 |

**关键架构约束**：云服务器**不跑 Codex**。Worker 只做**出站** HTTPS 轮询，服务器从不反连
Worker。这意味着 Worker 可以在任何家用网络里跑，不需要公网 IP。

进度：Phase 01–07 已完成。**Phase 08（部署）与 Phase 09（备份/监控）尚未开始**，
所以现在还不能对外提供服务。

---

## 二、依赖

### 必需

| 依赖 | 版本 | 用途 | 装不上会怎样 |
|---|---|---|---|
| Python | ≥3.12, <3.15 | 服务端 + Worker | 全部跑不了 |
| Node.js | ≥20（实测 26） | `miniapp/` 与 `admin/` 的测试与构建 | 前端跑不了，服务端不受影响 |
| Git | 任意近期版本 | — | — |

Python 依赖全在 `pyproject.toml`，一条命令装完（见第三节）。其中值得知道的几个：
`fastapi` / `uvicorn`（HTTP）、`sqlalchemy` + `alembic`（ORM 与迁移）、`pymysql`（生产数据库）、
`pypdf` + `PyMuPDF`（PDF 校验与页数）、`argon2-cffi`（Admin 密码）、`jsonschema`（批改产物校验）。

### 只有「真正跑批改」时才需要

| 依赖 | 用途 | 没有会怎样 |
|---|---|---|
| `codex` CLI（已登录） | Worker 调用 AI 批改 | **只影响真实批改**；`AI_GRADER_RUNNER_MODE=demo` 可用 `FakeGrader` 跑通全链路 |
| XeLaTeX（TeX Live / MacTeX） | 生成带批注 PDF | 同上 |

**重要**：跑测试**不需要** Codex、不需要 XeLaTeX、不产生任何 AI 费用。
`worker/runtime/doctor.py` 有八项环境自检，用 `python -m worker.cli doctor` 一次看清缺什么。

### 完全不需要（这是重点）

- ❌ **域名** — 见第五节
- ❌ **HTTPS 证书**
- ❌ **云服务器**
- ❌ **MySQL**（开发用 SQLite；只有 4 个并发锁测试需要真 MySQL，缺了会自动 skip）
- ❌ **微信 AppID / 支付资质**（有完整的假登录 + 假支付链路）

---

## 三、从零启动（约 10 分钟）

```bash
# 1. Python 环境
cd math-competition-grader
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'

# 2. 确认全绿（约 3 分钟）
.venv/bin/python -m pytest -q
# 期望：788 passed, 6 skipped

# 3. 本地配置
cp .env.example .env
# 然后按第四节把 .env 改成本地开发值

# 4. 建库
mkdir -p tmp/server-data
GRADER_DATABASE_URL=sqlite+pysqlite:///./tmp/dev.sqlite3 .venv/bin/alembic upgrade head

# 5. 起服务端
.venv/bin/python -c 'import uvicorn; from server.config import ServerSettings; from server.main import create_app; uvicorn.run(create_app(ServerSettings()), host="localhost", port=8000)'
curl http://localhost:8000/health/live    # {"status":"ok"}
```

前端：

```bash
cd miniapp && npm test     # 99 passed，纯 Node，不需要微信开发者工具
cd admin && npm ci && npm test && npm run build   # 64 passed；npm ci 从 lockfile 精确安装
cd admin && npm run dev    # http://localhost:5173
```

> `admin/node_modules` 没有进 git（144 MB），但 `package-lock.json` 进了，
> 所以 `npm ci` 会装出**完全相同**的依赖树。我已实测：clean `npm ci` 后 64 项测试与构建都通过。

---

## 四、环境变量

### 服务端（前缀 `GRADER_`，共 12 项，全部在 `.env.example` 里）

`server/config.py` 会自动读仓库根目录的 `.env`。

| 变量 | 本地开发建议值 | 说明 |
|---|---|---|
| `GRADER_ENVIRONMENT` | `development` | `development`/`test`/`staging`/`production`。**只有前三个会注册假登录/假支付** |
| `GRADER_DATABASE_URL` | `sqlite+pysqlite:///./tmp/dev.sqlite3` | `production` 下**强制** `mysql+pymysql://` |
| `GRADER_DATA_DIR` | `./tmp/server-data` | 上传的 PDF 与批改结果存这里 |
| `GRADER_SESSION_SECRET` | 任意 ≥32 字符 | 小程序会话签名 + Admin CSRF 派生 |
| `GRADER_WORKER_SHARED_KEY` | 任意 ≥32 字符 | Worker 认证；**必须与 Worker 侧一致** |
| `GRADER_ADMIN_SHARED_KEY` | 任意 ≥32 字符 | **已废弃**，Phase 07 起不再认证任何东西，留着只为兼容旧 `.env` |
| `GRADER_ADMIN_ORIGIN` | `http://localhost:5173` | **安全控制**：Admin 写请求的 `Origin` 必须与它**字面相等** |
| `GRADER_PRICE_CENTS_PER_PAGE` | `500` | 逐页精批兼容价格，¥5/答卷页 |
| `GRADER_SUMMARY_PRICE_CENTS_PER_PAGE` | `100` | 简明评分，¥1/答卷页 |
| `GRADER_ANNOTATED_PRICE_CENTS_PER_PAGE` | `500` | 逐页精批，¥5/答卷页；已有报价不受调价影响 |
| `GRADER_MAX_PDF_BYTES` | `26214400` | 25 MB |
| `GRADER_MAX_PDF_PAGES` | `30` | |
| `GRADER_QUOTE_TTL_SECONDS` | `86400` | 报价有效期 |
| `GRADER_ACCEPTANCE_TTL_SECONDS` | `259200` | 3 天验收期 |

**两个必须知道的坑**：

1. **Alembic 不读 `.env`**，只读进程环境变量。跑迁移必须显式带上：
   `GRADER_DATABASE_URL=... .venv/bin/alembic upgrade head`
2. **Phase 07 起部分参数改成「数据库优先、环境变量兜底」**：
   `MAX_PDF_PAGES` / `MAX_PDF_BYTES` / `QUOTE_TTL_SECONDS` / `ACCEPTANCE_TTL_SECONDS` 等
   可以在 Admin 设置页在线改，存进 `operational_settings` 表并**立即生效、无需重启**。
   表里有该键时环境变量就**不生效了**——排查「改了 `.env` 没反应」先查这张表。

### Worker 侧（前缀 `GRADER_WORKER_`）

| 变量 | 本地值 | 说明 |
|---|---|---|
| `GRADER_WORKER_SERVER_BASE_URL` | `http://localhost:8000` | **非 localhost 强制 HTTPS**，见第五节 |
| `GRADER_WORKER_SHARED_KEY` | 同服务端 | 两边必须一致 |
| `GRADER_WORKER_INSTALLATION_ID` | 任意唯一字符串 | 同一个 id 幂等返回同一个 worker_id |
| `GRADER_WORKER_WORKSPACE_ROOT` | `./tmp/worker` | 每个任务的隔离工作区 |

其余可选：`WORKER_ID` / `DEVICE_NAME` / `WORKER_VERSION` / `POLL_WAIT_SECONDS` /
`RENEW_INTERVAL_SECONDS` / `REQUEST_TIMEOUT_SECONDS` / `MAX_CODEX_SESSIONS_PER_JOB`。

> **前缀陷阱**（我在 Phase 07 修过一次）：`GRADER_` 是 `GRADER_WORKER_` 的前缀，
> 两个配置模型读同一个 `.env`。现在两边会各自忽略对方命名空间，但**仍然会对自己的拼写错误报错**
> ——`GRADER_MAX_PDF_PAGE`（少个 S）会直接启动失败，这是故意的。

---

## 五、⭐ 没有域名怎么测（重点）

**结论：域名只有「真机微信小程序 + 真实微信支付」才需要。其余 100% 可以本地测完。**

我已实测：在零域名、零证书的情况下三个入口全部可用。

```
health           200   miniapp 假登录  200
admin 登录       204   worker 注册     201
```

### 5.1 服务端 + Admin 控制台：完全不需要域名

Admin 会话 Cookie 是 `SameSite=Strict` + `Path=/admin`，开发环境**刻意不加 `Secure`**
（浏览器会拒收 http 下的 Secure Cookie）。Vite 已把 `/admin/api` 代理到后端，
所以浏览器视角下前后端同站。

```bash
# 终端 1：后端（注意 host 用 localhost）
.venv/bin/python -c 'import uvicorn; from server.config import ServerSettings; from server.main import create_app; uvicorn.run(create_app(ServerSettings()), host="localhost", port=8000)'

# 终端 2：Admin SPA
cd admin && npm run dev
# 打开 http://localhost:5173/admin/login
```

**必须都用 `localhost`，不要混用 `127.0.0.1`**。对 Cookie jar 来说它们是两个不同的主机，
混用会导致 Cookie 不回传，而且**只在开发环境出错**，极难排查。

建 Admin 账号（没有 CLI，Phase 08 才有运维入口）：

```bash
.venv/bin/python - <<'PY'
from server.db import create_session_factory
from server.models import AdminUser
from server.services.admin_sessions import hash_password
f = create_session_factory("sqlite+pysqlite:///./tmp/dev.sqlite3")
with f() as s:
    s.add(AdminUser(username="ops", password_hash=hash_password("换成你的密码")))
    s.commit()
PY
```

完整逐页验收清单在 **`admin/README.md`**。

### 5.2 小程序：微信开发者工具可以跳过域名校验

`miniapp/project.config.json` 已设 `urlCheck: false`，`miniapp/config.js` 的 `staging`
profile 直接指向本地后端。

1. 微信开发者工具导入 `miniapp/` 目录（AppID 可选「测试号」）
2. 右上角**详情 → 本地设置 → 勾选「不校验合法域名、web-view、TLS 证书」**
3. `config.js` 的 `staging.baseUrl` 改成 `http://127.0.0.1:8000`
   （小程序这边用 `127.0.0.1` 更稳，它和 Admin 的 Cookie 无关）
4. 用 `test-` 开头的 code 假登录，用 `simulate-success` 假支付

假登录/假支付/假回调三组路由**在 production 环境不注册**，所以这套只能打本地或 staging。

完整清单在 **`miniapp/README.md`**。

### 5.3 Worker：本机不需要域名，跨机器需要一个隧道

Worker 强制「非 localhost 必须 HTTPS」。我实测：

| `SERVER_BASE_URL` | 结果 |
|---|---|
| `http://localhost:8000` | ✅ 通过 |
| `http://127.0.0.1:8000` | ✅ 通过 |
| `http://192.168.1.50:8000` | ❌ **被拒**（局域网明文不行） |
| `https://grader.example.com` | ✅ 通过 |

所以：

- **Worker 和后端在同一台机器** → 直接用 `http://localhost:8000`，什么都不用配。
- **Worker 在另一台机器** → 不要改代码去放宽这个校验（它是故意的）。用 SSH 反向隧道
  把远端的 `localhost:8000` 映射过去：
  ```bash
  # 在 Worker 那台机器上执行
  ssh -N -L 8000:localhost:8000 你的用户名@跑后端的机器
  # 然后 Worker 仍然配 http://localhost:8000
  ```
  这也正是 Phase 09 计划里 staging 的访问方式（SSH 端口转发，只开 22 端口）。

不装 Codex/XeLaTeX 也能跑通全链路：

```bash
GRADER_WORKER_SERVER_BASE_URL=http://localhost:8000 \
GRADER_WORKER_SHARED_KEY=<与服务端一致> \
GRADER_WORKER_INSTALLATION_ID=dev-1 \
GRADER_WORKER_WORKSPACE_ROOT=./tmp/worker \
AI_GRADER_RUNNER_MODE=demo \
  .venv/bin/python -m worker.cli doctor      # 先看环境
#                                    register / run-once / run / status / drain
```

### 5.4 什么时候才真的需要域名

| 事项 | 需要域名？ | 现状 |
|---|---|---|
| 服务端所有 API | ❌ | 本地可测 |
| Admin 控制台全部功能 | ❌ | 本地可测 |
| 小程序全流程（开发者工具） | ❌ | 本地可测（跳过域名校验） |
| Worker 批改（同机） | ❌ | 本地可测 |
| Worker 批改（跨机） | ❌ | 用 SSH 隧道 |
| **小程序真机预览/体验版** | ✅ | 微信要求 request 合法域名必须 HTTPS 备案域名 |
| **真实微信登录（wx.login）** | ✅ | 还需 AppID + AppSecret |
| **真实微信支付** | ✅ | 还需支付商户资质 |

**优先级建议**：域名与微信资质申请周期长（备案通常数周），建议**立刻并行启动申请**，
但不要等它——Phase 08 除「Nginx/证书/域名切换」外的部分（systemd 单元、部署脚本、
日志轮转）都可以先做完。计划文档里这几项已被明确 gated。

---

## 六、怎么打包交给同事

### 推荐：交 Git 仓库本身（保留 74 个提交的历史）

历史很有价值——每个提交信息都写了「为什么这么做」和踩过的坑。

```bash
cd /Users/tim/Desktop/批改小程序
tar --exclude='math-competition-grader/.venv' \
    --exclude='math-competition-grader/.worktrees' \
    --exclude='math-competition-grader/node_modules' \
    --exclude='math-competition-grader/admin/node_modules' \
    --exclude='math-competition-grader/tmp' \
    --exclude='math-competition-grader/.env' \
    --exclude='__pycache__' \
    --exclude='.DS_Store' \
    -czf ~/Desktop/grader-handover.tar.gz math-competition-grader
```

实测 **73 MB**（含 `.git` 43 MB 与字体 39 MB，压缩后有重叠）。

### 或者：只交当前快照（实测 33 MB，无历史）

```bash
cd math-competition-grader
git archive --format=tar.gz -o ~/Desktop/grader-snapshot.tar.gz HEAD
```

### 更好的做法：推到私有远端

仓库目前**领先 `origin/main` 74 个提交，从未推送过**。如果公司有私有 Git（GitLab/CNB/Gitee），
直接推上去比传压缩包好得多：

```bash
git remote -v                      # 先确认现有远端指向哪
git push origin main               # 需要你确认后再执行
```

> ⚠️ 我没有执行 push，也没改动任何远端。要推请你自己决定推到哪个远端。

### 打包前务必确认

```bash
git status --short                 # 应只剩你自己的两个本地配置文件
grep -c . .env 2>/dev/null && echo "⚠️ .env 存在，确认它没进压缩包"
```

**绝对不要**打包进去：`.env`（含密钥）、`tmp/`（本地数据库与上传的 PDF）、
`.venv/`、`node_modules/`、任何学生答卷 PDF。`.gitignore` 已覆盖这些，
但用 `tar` 时要靠上面的 `--exclude`。

### 另外要口头交接的东西

1. `~/Desktop/数学竞赛题批改-source(1)/` 之类的**旧版批改器快照**（57 项测试通过）
   —— 它不是 git 仓库、不随主仓库演进，但**是真实批改效果的唯一验证基线**。
   里面 `data/jobs/` 含**真实学生答卷，不可外传**。
2. `.env` 里的密钥**不要**放进压缩包，用密码管理器单独给。
   其实本地开发随便生成新的就行（≥32 字符），只有生产环境的才需要交接。

---

## 七、后续开发要点

### 7.1 先读这三份

1. **`CODEBUDDY.md`** —— 项目现状、架构、**必须守住的安全不变量**（最重要）
2. **`.codebuddy/rules/`** —— 服务端编码约定、阶段纪律、旧版批改器边界
3. **`docs/superpowers/plans/README.md`** —— Phase 01–09 逐步计划

### 7.2 工作方式（前七个阶段都这么做的）

- **计划先于代码**：`docs/superpowers/plans/` 下每个 Phase 都有含失败测试和 commit 信息的详细计划。
- **TDD**：先写失败测试 → 确认它因缺功能而失败 → 最小实现 → 通过 → 提交。每个 Task 一个 commit。
- **`pytest` 是唯一自动化关卡**（没有 linter / formatter / type checker）。
- **对安全守卫做变异测试**：临时删掉守卫，确认对应测试真的变红。
  这在 Phase 07 逮到了 3 个「看起来在测、实际删掉守卫也不报错」的空洞测试——
  其中一个是我自己写的。**审查零发现时尤其要自己再抽查。**

### 7.3 绝对不要破坏的不变量（都有测试）

| 不变量 | 破坏后果 |
|---|---|
| 退款只有一条代码路径（`RefundService`），`external_refund_id` 唯一 | **真实双倍退款** |
| 一笔支付同时只允许一个存活退款行；`refunded` 后不得再退 | 同上 |
| 金额与收款方**绝不接受客户端传入** | 任意金额退款 |
| `lease_version` fencing：ACK/续租/上传/提交四条写路径都要校验 | 陈旧写入覆盖已交付结果 |
| `ix_grading_jobs_claim` 索引 | MySQL 上 `FOR UPDATE SKIP LOCKED` 返回空行，Worker 集体饿死 |
| drain/disable **不取消**正在执行的任务 | 丢弃用户已付费的批改运行 |
| 三认证域隔离（小程序 / Worker / Admin，**两个方向**） | 越权 |
| 状态变更必须走 `require_order_transition` / `require_job_transition` | 状态机失效 |
| 事务里不移动/删除文件（结果提交用 copy→commit→删暂存） | 任务永久卡在 `uploading` |
| 调价只新建 `price_rules` 版本，不回写 `quoted_amount_cents` | 改掉已成交价格 |
| 假登录/假支付在 production **不注册** | 任何人可伪造身份与支付 |
| Admin 响应不含 `relative_path` / `openid` / `installation_id` / 任何密钥 | 信息泄漏 |

### 7.4 已知遗留风险（`CODEBUDDY.md` 有完整列表）

- **`GRADER_ADMIN_SHARED_KEY` 已废弃但字段还在** —— Phase 08 应删除它和 `.env.example` 那行。
- **Admin 登录限流是进程内的** —— 多进程部署会让 5 次阈值放大成 N×5。横向扩容前要换共享存储。
- **退款走 `FakePaymentGateway`** —— 接真实微信退款时，必须确认对方**真的按 `external_refund_id` 去重**，
  幂等性依赖这一点。
- **假支付回调无签名** —— 换真实支付必须加签名校验。
- **scheduler 需要有人真的跑起来**（`python -m server.scheduler.main`），
  Phase 08 才有 systemd 单元。不跑就没有清理，磁盘会一直涨。
- **`verify_backup_freshness` 是占位**，返回 `skipped`；真实备份是 Phase 09。
  资金页也刻意**不声称**银行已结算——没有对账单就不编数字。
- **支付回调路径未在真实 MySQL 上验证过**（Phase 03 的原子 claim 验过了）。
  SQLite 静默忽略 `FOR UPDATE`，行锁正确性无法在 SQLite 上证明。
  跑真 MySQL 测试：`GRADER_TEST_MYSQL_URL="mysql+pymysql://root@127.0.0.1:3306/grader_test" .venv/bin/python -m pytest tests/integration -q`

### 7.5 下一步做什么

**Phase 08（部署与真实适配器）**，读 `docs/superpowers/plans/2026-08-08-phase-08-*.md`。
其中不依赖域名的部分现在就能做：systemd 单元、部署脚本、日志轮转、
删除废弃的 `admin_shared_key`。Nginx / certbot / 域名切换 / 真实微信支付已被 gated，等资质。

**待人工验收**（我没做，不要假设已完成）：
- `admin/README.md` 的逐页清单，特别是**价格版本化**（调价后旧报价金额必须不变）
- `miniapp/README.md` 的真机清单，特别是**退款后下载被拒**（应返回 410）

---

## 八、当前测试基线（接手后应能复现）

| 命令 | 期望 |
|---|---|
| `.venv/bin/python -m pytest -q` | **788 passed, 6 skipped** |
| `cd miniapp && npm test` | **99 passed** |
| `cd admin && npm ci && npm test` | **64 passed** |
| `cd admin && npm run build` | 成功 |
| `cd admin && npm run test:e2e` | 4 passed（需 `npx playwright install chromium` + 后端） |

6 项 skip 都需要外部环境：4 项需 `GRADER_TEST_MYSQL_URL`，2 项按宿主平台跳过。
**这 6 项 skip 是正常的**，不要试图让它们变绿。

Alembic head：**`0006`**。改 schema 一律新增 migration，不回写 `0001`–`0006`。

如果 `pytest` 不是 788，先怀疑环境（Python 版本、依赖版本），再怀疑代码。
