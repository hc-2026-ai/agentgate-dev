# 行内完整取证与真实调度验收

本次补齐行内 Trace 查询和 BJS 失败处理。Skill 静态分析按用户要求暂缓：真实行内版本定义接口尚未提供，不使用本地 `/web/agent/capabilities` 扩展冒充。

## 云虾 Skill 路由取值 — 2026-10-26

云虾每轮最终 `message` 响应中的 `intent_code` 直接作为该轮路由评估值，
写入标准 Trace 的 `routing.selected_skill`。样本的 Skill 路由期望值应填写对应的
`intent_code`。该记录表示智能体报告的意图，不证明 Skill 内部执行成功。

- 移除 `read_file`、`SKILL.md` 路径及 Skill 目录元数据推断，以及相应的路径、名称和歧义校验。
- 各轮只使用本轮响应；缺失、空白或非字符串的值不生成路由，不复用上一轮，也不从文件路径兜底。
  此类缺失不会抛出新的执行异常；本轮配置了 Skill 路由期望时，按“未产生路由决策”判失败。
- Skill 路由评估不依赖 Trace 查询是否成功：有 `intent_code` 就比较，无有效值就按缺失处理。
  工具、参数等依赖过程记录的评估仍区分 `trace_server` 与 `output_only`。
- 基础编排与工作流不生成 Skill 路由。`read_file` 仍是普通工具调用，保留其原始输入、输出及参数。
- 行外模式和通用路由评估器的比较规则保持原有协议。

## Yunxia per-turn request fields — 2026-10-17

云虾没有 `init_session`。请求字段统一填写在每轮 `input`，不使用 Case 的
`initial_state`，也不新增云虾初始化表单。普通文本可直接填写；勾选
**结构化 JSON 输入** 后，最小输入同样只需：

```json
{"txt":"查询年休假政策"}
```

完整输入示例（除 `txt` 外均为选填）：

```json
{
  "txt": "查询年休假政策",
  "appHistory": [{"role": "user", "content": "之前的问题"}],
  "custID": "hk_yat",
  "config_variables": []
}
```

- `txt` 必须是非空字符串；作为普通文本填写的 JSON 原文不会再次解析或编码。
- `appHistory` 未填或为 null 时使用 `[]`；其他 JSON 值沿用客户脚本的历史归一化逻辑。
- `custID` 未填、为 null 或空白时自动生成。同一 Case 的默认客户标识稳定，
  不同 Run 使用不同随机后缀；显式客户标识也追加运行级后缀，但不要求测试前缀。
- `config_variables` 未填或为 null 时使用 `[]`，填写时保留数组内容，不额外限制条目结构。
- `sessionId` 选填；同一 Case 有显式值时各轮共用该值，否则使用 Case ID。
  同一 Case 不得填写多个不同的会话标识。

各轮分别读取 `custID`、`appHistory` 和 `config_variables`；如需多轮使用同一自定义值，
请在对应轮次填写。以上字段分别组装到 `/api/v1/message` 的请求顶层，不会把整个
输入对象塞入 `txt`。`executionMode=execute`、`stream=true`、`debugTrace=true`
保持客户脚本的固定值。附件上传不在本次范围内，行外模式保持原协议。

## ChatABC business inputs — 2026-10-17

This section applies to in-bank workflows and base orchestration. External mock
execution, Trace collection and scoring retain their existing behavior.
No database migration or attachment upload is included.

In the sample editor:

1. Expand **初始变量**, select **工作流** or **基础编排**, click **添加变量** and fill each variable's name and
   value. This optional section starts collapsed; collapsing it does not clear values.
   New values are saved as text, so identifiers such as `810000` keep their string
   type. There is no value-type selector. Existing non-string JSON values remain
   editable as JSON and keep their types on save. Use the names and required fields
   specified by the selected Agent; no customer-specific defaults are inferred.
   Rows can be removed, and
   no rows are needed for Agents without initialization variables. Completely
   blank added rows are ignored on save; named variables with empty string values
   are preserved. Published versions and conversation previews are read-only. The stored representation
   uses `initial_state.config_variables` for workflows, or `initial_state.prompt_variables`
   and `initial_state.tool_variables` for base orchestration. Saving retains only the
   selected initialization type's fields. The workflow example is shown below.
   The **背景知识** and **自定义变量** annotation fields are no longer shown in this
   editor; existing saved extensions are preserved and are not sent to the Agent.
2. For each turn, use ordinary text or enable **结构化 JSON 输入** and enter the
   complete business object, not an HTTP envelope or a JSON-escaped string.
3. Save, publish the dataset, select the matching in-bank Agent/version and submit the task.
   Different cases initialize independent sessions; turns in one case reuse its session.
4. Check the actual initialization request and each chat request, then inspect the
   per-turn Trace and evaluation results. Reruns retain the published input snapshot.

The canonical sample fields are:

```json
{
  "name": "Workflow translation example",
  "initial_state": {
    "config_variables": [
      {"name": "centreId", "value": "810000"},
      {"name": "userId", "value": "test-user"},
      {"name": "bankNo", "value": "810000"}
    ]
  },
  "turns": [
    {
      "input": {
        "user_input_text": "请翻译 acquisition",
        "menuId": "TRANSLATE",
        "lang_src": "auto_detect",
        "lang_tar": "fr_FR",
        "form": {"items": [0, false, null]}
      }
    },
    {"input": {"user_input_text": "换一个例子", "menuId": "TRANSLATE"}}
  ]
}
```

`initial_state.config_variables` is sent once in `init_session.data.config_variables`.
Base orchestration sends `initial_state.prompt_variables` and `initial_state.tool_variables`
once in the corresponding `init_session.data` fields. Both types use the same input
conversion: a complete business `input` object is JSON-encoded once into `chat.data.txt`.
Nested objects, arrays, numbers, booleans and nulls are preserved; no question-only
field is extracted. Existing text envelopes (`{"txt":"hello"}`, optionally with
empty `files`) remain plain text. Inputs containing only `txt` / `files` are treated
as text envelopes; enter a pre-encoded JSON string as text if those are the business
object's only keys. Nonempty transport attachments are rejected.
With **结构化 JSON 输入** unchecked, ordinary text and JSON source text are both sent
unchanged in `chat.data.txt`; the source text is not parsed or encoded a second time.

The runtime owns the HTTP envelope, authentication, session ID and request ID.
Business fields never overwrite those transport fields. The existing **背景知识**
and **自定义变量** annotations are not sent to the Agent.

JSON and Excel exports preserve the Case configuration and each turn's complete input.
The Excel columns remain `initial_state_json`, `input_json`, `case_id`, and `turn_order`;
no separate column is required for each Agent's business fields.

Local acceptance uses a temporary SQLite database and simulated customer responses.
It verifies both passing and deliberately failing expectations and unchanged rerun
snapshots; this does not replace real in-bank acceptance.

## 配置

已有行内网关、数据库、登录认证、执行器配置保持不变，另外设置：

```dotenv
AGENTGATE_AGENT_PLATFORM_MODE=inbank
AGENTGATE_TRACE_SERVER_URL=https://your-trace-host
AGENTGATE_INBANK_TRACE_PROJECT_ID=your-project-id
AGENTGATE_TRACE_SERVER_TOKEN=your-private-trace-query-token
```

将示例替换为真实配置。Trace URL 只允许 origin，不含路径、账号或查询参数；服务不要求认证时可不设查询 Token。该 Token 与行内登录令牌独立，不会把用户会话令牌发送给 Trace Server。必须显式提供项目 ID 和 URL；配置缺失会在创建 Pod 前失败。行外使用原有本地配置，不读取行内项目 ID。

## 被测平台需满足的协议

ChatABC 基础编排、工作流和云虾每轮正常响应都必须携带唯一的 SSE 引用：

```text
event: trace
data: {"project_id":"your-project-id","trace_id":"source-trace-id","session_id":"actual-session-id","request_id":"actual-request-id"}

```

该帧须在 `done` 前发送，且证据已经可查询。`project_id` 和 `trace_id` 必填；可选的 session_id/request_id 若出现必须匹配本次实际调用。项目/Trace 标识为最多128字符的字母数字、点、下划线和连字符，首字符为字母或数字。SSE 中的 URL 不参与请求寻址。

测评端读取以下既有 Trace Server 查询接口：

```text
GET /api/v1/projects/{project_id}/traces/{trace_id}
GET /api/v1/projects/{project_id}/traces/{trace_id}/llm_requests
```

详情响应沿用仓库 `TraceServerClient` 的 camelCase 契约：`trace`、`spans`、`observations`；模型附件响应为 `items`。根 Trace 要有匹配的项目、Trace、sessionId、agentName（本次创建的 Pod 名）、输入文本和最终回答；状态为 success，spanCount 为实际完整 Span 数量。每条 Span 具有有效时间、状态和父子关系。实际返回的 LLM 请求附件会保留。缺失、冲突或非法证据明确报错，不回退为 output_only。各轮使用不同源 Trace ID。

SDK 支持的 agent/chain/llm/tool/retriever 等原始步骤会保留，Skill/工作流的名称和层级以远端实际 Span 为准；不能由静态图谱猜测执行路径。只有服务实际提供的字段可以展示，不宣称恢复上游查询面未暴露的数据。云虾继续保留 SSE 返回的意图、槽位和 workflow_calls，不把它们伪造为内部执行 Span。

本次没有修改客户 Agent 的代码，也没有让未上报的 Agent 自动具备上报能力。如果真实网关未实现上述 Trace 引用或查询契约，需要平台方提供真实协议并接入。网络取证发生在每轮回答后，不是运行中的实时 Span 流。查询失败不重放对话，Pod 仍按既有生命周期清理。

## 可复现的本机契约验收

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_inbank_evidence.py tests/test_target_execution.py \
  tests/test_target_execution_factory.py tests/test_trace_server_client.py \
  tests/test_bjs_dispatcher.py tests/test_bjs_scheduling.py tests/test_bjs_execution.py
```

执行测试使用模拟行内网关及独立的本地 HTTP Trace 查询服务，覆盖基础编排、工作流、云虾、多样本多轮、LLM 附件、父子关系、错误会话/项目/输入/输出、不完整证据、服务不可用和 Pod 清理。这是接口契约验收，不代表真实行内平台通过。

## 真实行内验收

1. 配置自己的行内服务；用有效行内 Token 登录，选择真实智能体、版本和云虾分支。
2. 使用两轮且输入不同的样本，串行执行、重试0；先使用最终输出评估器验证连通性。
3. 在样本详情查看每轮下的模型和工具等内部环节及 JSON；应包含 `inbank.evidence_mode=trace_server`、实际源 `trace_sdk.trace_id`，`trace_sdk.replay=false`。点击节点应定位对应 JSON。
4. 根据真实返回的工具/路径证据配置过程规则；不能用本地贷款工具名称直接假设客户平台行为。
5. 测试环境中让一条 Trace 引用失效或让查询服务拒绝访问，确认任务失败且不会重复执行业务对话。只有回答、没有内部证据不算验收通过。

## BJS 失败验收

真实响应 `{code:"0",message:"success"}` 才确认受理；网络异常、超时、HTTP 错误、非法 JSON 和业务拒绝均报错。不再生成模拟成功响应，也不将响应中的敏感正文输出到错误信息。

传输层不重试；现有调度器使用同一 Run ID 有界重试：首次失败进入 WAITING，达到 `AGENTGATE_MAX_DISPATCH_ATTEMPTS` 后为 FAILED。超时只能表示受理未确认，无法证明远端没有执行；BJS 端应按 taskId 去重，不能声称跨系统 exactly-once。远端取消接口仍未定义，本次不宣称支持远程取消。

在测试 BJS 地址返回503、超时和业务拒绝，分别确认任务不会被误判受理；然后恢复服务，核对同一 taskId 的真实执行状态。不要在生产环境人为制造故障。
