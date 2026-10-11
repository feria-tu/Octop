# 验收记录

## 已通过

- `uv run pytest tests/integration/test_postgresql_runs.py tests/unit/agents/test_memory_slim.py tests/unit/agents/test_run_graph.py tests/unit/db/test_runs.py tests/unit/db/test_run_retention.py tests/unit/db/test_run_schema.py tests/integration/test_run_protocol.py -q --tb=short`：79 passed。使用临时 PostgreSQL 16 实例，验证真实图、双审批、重新创建服务后的检查点恢复，以及并发领取/取消 fencing。临时容器已删除。
- 最后追加的跨 Run 不确定写阻断、保留保护、OpenAPI schema 和协议回归：`uv run pytest tests/unit/db/test_run_retention.py tests/integration/test_run_protocol.py tests/unit/db/test_runs.py -q --tb=short`：22 passed。
- 真实参数改写绕过和首次工作区回填版本失败均先观察红，再修复为绿。子智能体治理、历史检查点头指针、断开恢复观察连接也覆盖真实图。
- `uv run ruff check src tests`：通过。
- `uv run ruff format --check src tests`：1188 files already formatted。
- `uv run mypy --strict src/octop`：576 files，通过。
- `git diff --check`：通过。
- 前端 `npx tsc -b`、`npm run build`：通过，生产资源已生成。
- 前端事件游标与审批行为定向测试：8 passed。

## 全量回归限制

Windows 没有 make，使用对应检查命令；未运行会写全仓格式的 make format-all。

后端 `uv run pytest -m 'not live' -n 8 --dist worksteal -vv --tb=short`：4270 passed，144 skipped，11 failed。其中三个 memory_slim 夹具通过 __new__ 构造 manager，未初始化新增 run_service；已修复夹具，后续 79 项验收包含该文件并全部通过。剩余八个 vendor_wheels_argv 测试在本 Windows 的 Bash/GBK 子进程环境失败；单独重跑仍为 8 failed，相关测试与脚本未修改。

前端全量 `npm test -- --run`：1115 passed，43 failed，30 failed files。包括缺失 mock、API 参数断言和超时；未逐项确认归因，不声称全套通过。全量 ESLint 为两个已有错误及 68 warnings，涉及旧 Boolean 表达式和未使用导入。

## 交付边界

没有提交、推送或部署。迁移 22 已在测试 SQLite 和临时 PostgreSQL 应用，未声称用户运行数据库已升级。验收未调用真实企业业务系统。

带 Run 的会话禁止永久删除，以保存业务账本与检查点；外部操作的不确定结果需要管理员核对，不能保证外部系统严格恰好执行一次。详细使用方法见 docs/durable-runs.md。
