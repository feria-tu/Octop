# 持久执行与业务工具治理

每次员工对话执行都会创建持久 `run_id`。Run 保存所属用户、智能体、线程、执行配置版本、租约、执行代次、部分输出和最终结果。SQLite 与 PostgreSQL 使用同一语义，数据库迁移版本为 22。

## 生命周期

状态包括 `queued`、`running`、`waiting_approval`、`recovery_blocked`、`succeeded`、`failed`、`cancelled`。同一智能体和线程最多有一个活动 Run。租约失效后进入 `recovery_blocked`；取消立即增加执行代次，旧执行不能再保存事件、检查点或最终结果。执行期间会重新验证账号、智能体访问权限和配置版本。版本改变后需要开启新执行，不能用新配置继续旧检查点。

`GET /api/runs/{run_id}` 返回状态、结果、审批和写账本；`GET /api/threads/{thread_id}/run` 返回最新 Run。普通用户只能访问自己的执行。管理员通过 `GET /api/admin/runs/{run_id}` 审计查看，通过 `POST /api/admin/runs/{run_id}/cancel` 取消；管理员查看写入审计记录。

用户取消使用 `POST /api/runs/{run_id}/cancel`。取消不等于撤销已发送到外部系统的业务操作，正在执行的写调用会被标记为不确定。

## 工具授权与审批

外部插件和 MCP 业务工具默认拒绝执行。管理员先通过 `PUT /api/business-tools/{name}` 注册工具，提交 `{"read_only":false,"enabled":true}`；再通过 `PUT /api/business-tools/{name}/grant` 授权具体智能体和员工，提交 `{"agent_id":"...","user_id":1,"enabled":true}`。工具名必须与实际模型工具名一致。

`GET /api/business-tools` 和 `GET /api/business-tools/{name}/grants` 可查看当前配置。`read_only` 是管理员对真实工具行为的分类，不能把有副作用的工具标记为只读。

每个写调用保存独立审批，绑定 Run、调用 ID、工具名、最终规范化参数摘要和授权策略版本。插件改写参数后再进行审批检查。审批不会被会话级“全部允许”绕过，子智能体也受相同治理约束；无法验证治理的预编译子智能体会被拒绝。

审批卡片携带 `approval_id`、`args_digest` 和 `run_id`。`POST /api/runs/{run_id}/approval` 接收 `{"approval_id":"...","args_digest":"...","approved":true}`；随后调用 resume，或直接通过审批卡片提交对应决策。修改参数、错配调用、授权撤销和旧策略版本都会拒绝执行。

## 写账本与恢复

写操作先保存 `started` 账本，再调用外部工具。只有成功且绑定原调用的工具返回消息才能保存为 `succeeded`。超时、取消、进程退出、异常或失去租约会保守视为 `uncertain`，阻止自动重试；同一会话也不能新建 Run 绕过未核对的写操作。

管理员核对外部系统后调用 `POST /api/runs/{run_id}/writes/{call_id}/resolve`：

- 确认未执行：`{"outcome":"not_executed","note":"核对依据"}`。
- 确认成功：`{"outcome":"succeeded","note":"核对依据","result":{"type":"tool","content":"业务结果","tool_call_id":"原调用ID","status":"success"}}`。

恢复使用 `POST /api/runs/{run_id}/resume`，请求体 `{"decisions":[]}`。已领取的执行从持久图检查点继续，不重新提交用户输入；尚未开始的 queued Run 使用原始保存输入。成功账本直接回放结果，不重复调用外部工具。只有最新 Run 可恢复；有未核对写操作时继续阻断。恢复任务独立于 SSE 观察连接，关闭浏览器连接不会终止恢复任务；显式取消仍会终止执行。

这些机制不能替外部系统提供严格的“恰好执行一次”。外部操作已成功、但成功账本尚未保存的窗口需要人工核对；接入方可以额外使用外部系统支持的幂等键。

## 事件与最终结果

每个 Run 的事件序号 `seq` 单调递增，事件先持久化再发送。`GET /api/runs/{run_id}/events?after_seq=0&limit=100` 可分页补拉，使用返回的 `next_seq` 和 `has_more`。

WebSocket 订阅可携带 `run_id` 和 `after_seq`，按数据库日志补齐后继续监听，包括其他进程写入的事件。客户端以 `(run_id, seq)` 去重；现有聊天前端已保存会话内游标并在重连时补拉。页面完全刷新后可通过线程 latest Run 接口重新获取执行标识。

最终结果、终态和唯一终态 `done` 事件在同一数据库事务提交。重复提交相同结果返回原终态事件；等待审批或恢复阻断时的传输结束不会冒充成功提交。

## 验证范围

已有持久 Run 的会话目前禁止永久删除，返回冲突，避免连带删除业务账本和恢复检查点。智能体或账号的管理员删除仍遵循现有级联生命周期；本实现不提供独立于账号和智能体生命周期的永久审计归档。

验收使用真实 LangGraph/Deep Agents 图与可控假模型，验证调用和持久化边界；同时覆盖 SQLite 并发和临时 PostgreSQL 实例。不包含真实企业业务系统的执行证明或生产部署验证。API 结构可在 `/api/docs` 查看。
