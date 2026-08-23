---
description: 运行阶段验收门：全量测试 + 工作区改动检查
allowed-tools: Bash
---

执行本仓库的阶段 gate 检查，并报告结果：

1. 运行 `.venv/bin/python -m pytest -q -rs`
   （当前基线：**392 项通过 + 2 项跳过**；`tests/server/` 370、`tests/worker/` 19、
   `tests/integration/` 3 通过 2 跳过）
2. 运行 `git status --short`
3. 运行 `git diff --stat`

然后判断并汇报：

- 是否全部测试通过。若有失败，列出失败的测试节点，不要自行修改代码。
- 改动的文件是否都属于当前正在推进的阶段范围。若有越界改动（例如在做 `server/` 阶段却动了
  `worker/`，或引用了尚不存在的 `miniapp/` `admin/` `ops/`），明确指出。
- 如果通过数少于 392，说明有测试被删除或跳过，需要指出。
- **跳过数应恰好为 2**（`tests/integration/test_mysql_job_claim.py`，缺 `GRADER_TEST_MYSQL_URL`）。
  多于 2 项跳过要指出是哪些、为什么。
- 若本阶段改了 schema，检查是否新增了 Alembic migration 而非回写已有版本（当前 head `0003`）。

只做检查和汇报，不要修改文件，不要 commit。
