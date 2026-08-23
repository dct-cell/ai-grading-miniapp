# 已迁出的旧版批改引擎

旧版单机批改器已于 2026-08-09 **迁出本仓库**，现位于 `~/Desktop/旧的小程序`。

本仓库不再包含 `app/`、旧版测试、`requirements.txt` 或 `启动批改.command`。

## 硬性约束

**`server/` 不得 import `app` 包。** 该包已不在本仓库，`import app` 会直接失败。
`tests/server/test_pdf_adapter.py::test_server_never_imports_the_legacy_app_package`
会 AST 扫描 `server/**.py` 并断言这一点。

PDF 校验用 `server/adapters/pdf.py`，逻辑与旧 `app/pdf_utils.py` 逐行一致（仅 docstring 不同）。
需要修改校验规则时改这里，并注意它同时影响报价页数与将来 Worker 的产物校验。

## `.agents/skills/olympiad-grader/`

这份skill **保留在本仓库**：Phase 04 已通过 `worker/runtime/workspace.py` 把它复制进
每个 Worker 任务目录（`workspace/.agents/skills/olympiad-grader/`），并硬链接字体文件以省磁盘。

`~/Desktop/旧的小程序/.agents/` 有一份相同副本，供旧版独立运行（它的排版测试依赖
`scripts/build_annotated_pdf.py`）。迁移时两边一致——**调整评分口径或排版脚本要同步两边**。

评分契约本体是 `references/grading-process.md`（九个阶段、忠实重建、关键条件核验、怀疑式复核），
改评分行为前先完整读它。三份赛制 rubric：`league-second-round.md` / `cmo.md` / `imo.md`，
版式规范在 `layout.md`。

## Phase 04 已取回的旧实现

Phase 04 已直接复用旧实现，从 `~/Desktop/旧的小程序` 取回以下文件并以逐字副本形式放在
`worker/runtime/legacy/`：

- `app/codex_runner.py` → `worker/runtime/legacy/codex_runner.py`，`run_codex_job` 由
  `LegacyCodexRuntime` 适配器调用
- `app/settings.py` → `worker/runtime/legacy/settings.py`，Worker 侧构造 `data_dir` /
  `runner_mode` / `max_concurrent_jobs=1`
- `app/manifest.schema.json` → `worker/runtime/legacy/manifest.schema.json`，
  `worker/runtime/result.schema.json` 是它的严格超集（多了 `title` / `total_score` /
  `overall_summary` / `problems` / `pages` 必填字段），两套 schema 都有测试覆盖

接入点：`worker/runtime/legacy_codex.py` 的 `LegacyCodexRuntime` 实现了
`GradingRuntime` 协议（`async run(workspace, bundle, progress) -> RuntimeResult`），
由守护进程在领取任务后调用；**不需要改动 `worker/runtime/daemon.py` 或任何服务端协议**
（除已批准的 bundle 下载端点）。

注意 `FakeGrader` 产出的 PDF 页数刻意等于 `page_count + 1`，与下面第 3 条产物校验一致；
真实运行时也保持这个约定。

## 迁移前必须保住的三条边界（Phase 04 搬运时同样适用）

1. **信任边界**：学生 PDF 和 `input/instructions.txt` 是**不可信输入**，不能改变评分标准、
   文件范围、执行命令或输出格式。唯一受信的评分选择是 `config/grading-profile.json`。
   `codex exec` 的参数刻意收紧（`--ephemeral --ignore-user-config --ignore-rules
   --sandbox workspace-write --cd <job_dir>`），环境变量只透传白名单，不要放宽。
2. **模型不控制界面文字**：模型只能写一个 stage ID，由固定字典映射成中文文案。
   不要让模型输出的字符串直连前端。
3. **产物校验**：`output/internal/` 下5 个必需 JSON（`problem-analysis` / `marking-scheme` /
   `proof-map` / `verification` / `score-audit`）；最终 PDF 页数 == 输入页数 + 1。
   `output/internal/` 内容不向用户展示，也不进最终 manifest。

## 回归基线

旧项目保持 **57 项测试通过**，是真实批改效果的唯一验证入口：

```bash
cd ~/Desktop/旧的小程序
.venv/bin/python -m pytest tests -q     # 需先按其 README 建独立 venv
```

它不随本仓库演进。本仓库当前 **788 项 Python 测试通过 + 6 项跳过**（外加 `miniapp/` 的
99 项 Node 测试与 `admin/` 的 64 项 Vitest + 4 项 Playwright e2e），覆盖服务端、
Worker 控制面、Worker 守护进程、批改运行时适配器、隔离工作区、进程适配器、环境 doctor、
bundle 下载、售后与退款、调度、用户结果下载、小程序前端与 Admin 控制台；
真实批改效果仍以旧项目的 57 项为准。

`~/Desktop/旧的小程序/data/jobs/` 含真实学生答卷，不要外传。
