# AgentGate Project Progress

Last updated: 2026-10-19

## 2026-11-02 — Yunxia post-health startup delay

Implement the customer-requested startup buffer in the existing Yunxia adapter:
after a newly created Pod passes its health check, wait before starting Case
execution. `AGENTGATE_INBANK_YUNXIA_POST_HEALTH_SLEEP_SECONDS` defaults to 15 seconds;
0 disables it and fractional seconds are supported. Reuse the existing injected
sleep function and log the delay's start and completion with Run and Pod identifiers.
The delay runs once per Pod, before the first Case, not once per Case or Turn.
It belongs to Pod preparation; existing health and conversation timeout budgets
retain their current meanings. Both in-bank and integrated tasks use this adapter;
ChatABC base/workflow and external targets do not gain a delay.

The production change is confined to `yunxia.py` and the environment sample.
Focused regression tests use a fake clock; shared test helpers explicitly disable
the new delay to preserve existing timing assertions. Five related backend modules
passed **673 tests**, with **28 skipped**. Six HTTP tests required a separate run
with local loopback binding enabled after the sandbox denied opening their test
server. Ruff and whitespace checks passed. Coverage includes default and configured
delays, zero disablement, health-before-delay-before-message ordering, reuse across
two Cases with two Turns each, cleanup after failed health checks, unaffected
ChatABC execution, and independent execution timeout budgets. These are local
contract tests; customer in-bank acceptance has not been performed.

## 2026-10-19 — Restore complete sample expectations

The current sample page replaces the two simplified state/tool controls with the
reference backup's Add expectation workflow: final state, tool arguments and final
output. Adapt the existing ExpectationEditor and reuse the current domain, dataset
API, publication and rule execution contracts. All rules stay configurable and
removable after reload. The basic expected-output field shares one equality-rule ID
with the complete editor, so editing either does not duplicate or erase checks.

The full editor preserves unfinished JSON drafts, validates required references and
numeric conditions, edits membership as a JSON array, and exposes JSON Schema.
Overflowing JSON numbers are rejected rather than silently serialized as null.
Fix `Equals.expected` and `OneOf.allowed` JSON serialization of frozen nested
objects/arrays while preserving immutable domain values. No schema migration or
Agent directory/token routing change is introduced.

The retained `examples/expectation-rules/` dataset contains a two-turn sample with
state, nested tool arguments and final-output checks, along with synthetic pass/fail
traces, a replay script and deterministic results. HTTP contract tests cover authoring,
editing, persistence, publication and export; RunManagement/RunEngine tests create
and execute real local tasks and read persisted reports, including an immutable v1
task snapshot after publishing v2. This is isolated acceptance with synthetic evidence,
not a live in-bank or external Agent execution.

Verification: related backend regression **245 passed**; the actual sample editor and
manual-create wizard browser suite **41 passed** with the production theme, desktop
and narrow-screen layout checks; the shared editor's older CaseEditor entry has
**3 passed** isolated browser regressions (44 browser cases total). Existing frontend
unit tests **112 passed**, and
the output helper suite including the new overflow case **6 passed** (113 distinct
unit cases overall). Type checking, affected ESLint and production build pass; the
existing large-bundle warning remains. Other-field regression covers initialization,
structured input, classification, multi-turn data, expected Skill and tool constraints,
hidden notes/extensions and unchanged expectation identities. The user subsequently
authorized committing and pushing this verified change to `integration/baibo1017`.

## 2026-10-19 — Integration with baibo1017, including PR #57

The user authorized delivering both PR #57 (head `98ff389`) and the execution-parity
work (`dbe00ca`) to `open-fin-sub/agentgate:integration/baibo1017`. Merge the
destination head `a7cbbaf` without rewriting either branch's history. Preserve its
evaluator groups, dataset/rule editing, task/report changes, and in-bank platform-only
target selection alongside the complete PR #57 initialization and structured-input
features, frontend-owned Case concurrency, failure isolation, timeout settings, and
customer final-message output handling. Attachments remain outside this delivery.

Resolve four textual conflicts in SampleEditor, its browser tests, platform task
tests, and this progress log. Keep both test suites and progress histories; adapt the
initial-variable controls to the destination's removal of conversation preview, preserve
hidden historical output conditions, and combine the test helper's mode and Case inputs.
No additional customer payload validation or production module is introduced by the merge.

Merged-tree verification: backend **2653 passed / 85 skipped**; frontend unit tests
**112 passed**; platform browser tests **80 passed**, sample-editor browser tests
**31 passed** after updating the obsolete comparison against the removed advanced
foldout. Vue type checking, affected-component ESLint, production build, and diff
whitespace checks passed. Existing large-bundle warnings remain. These are local
regressions; actual in-bank/BJS deployment acceptance is still required.

## 2026-10-19 — Frontend-owned concurrency and inline customer-style workers

The user superseded the backend-environment approach below: ordinary, stability,
and in-bank A/B submissions should use the page's existing `max_parallel_cases` value.
Both in-bank A/B variants use the same submitted value. External/mock A/B stays fixed
at 1. After the application checkpoint, the user explicitly authorized implementing
the remaining files together. The existing page control is restored; in-bank A/B also
allows editing and submits the same field. HTTP A/B accepts it with default 1 and the
existing 1–30 range. Ordinary submission retains its original required field. Remove
`AGENTGATE_INBANK_MAX_PARALLEL_CASES` from the environment example and runtime readers.
Run manifests, dispatch, credentials, output handling, and rerun snapshots are reused.
No new customer business-field validation is added.

The new shared `inbank/execution.py` is removed. Each existing ChatABC/Yunxia adapter
directly owns its bounded `ThreadPoolExecutor`, futures, cancellation, and deadline.
The customer scripts' session-group pool strategy is adapted, not replaced by a new
scheduler: independent Cases run concurrently; Turns stay ordered, as do Yunxia Cases
that explicitly share a session. Keep single-concurrency synchronous execution,
partial failure evidence, scoreless ERROR handling, request payloads, complete message
output, and joining workers before deleting the Pod. Customer Excel, batch database,
progress, and export orchestration are not copied into the platform. No new production
module or wrapper class is introduced. The declined malformed-error-marker guard stays
excluded. Submission to `open-fin-sub/agentgate` branch `integration/baibo1017` is now
authorized; integration must preserve the target branch's changes. No deployment or
real in-bank acceptance has been performed.

Verification: full backend regression **2549 passed / 58 skipped**; frontend unit
tests **91 passed**; focused browser tests **23 passed**. Vue type checks, production
build, affected Ruff/ESLint checks, and `git diff --check` passed. The build retains
the existing large-chunk warnings. Coverage includes
three agent modes, ordinary/stability/A/B caller values, actual bounded worker overlap,
Turn/session ordering, cancellation/cleanup, ignored obsolete environment values,
immutable saved manifests, and unchanged external routing. These are local tests,
not in-bank acceptance.

## 2026-10-19 — Unused concurrency argument cleanup

Removed the unused `max_parallel_cases` argument from the private target-snapshot
helper and its two callers; the actual submission, manifest, and adapter concurrency
values are unchanged. The user declined the optional incomplete-error-marker guard;
that guard and its added tests have been withdrawn. Existing execution-failure ERROR
handling remains unchanged. No customer business fields, HTTP payloads, or Domain/Trace
schemas are changed. No commit, push, or real in-bank deployment has been performed.

Verification after withdrawing the optional guard: focused platform-submission,
in-bank execution, evaluator, and Engine regression **374 passed / 28 skipped**;
affected Python Ruff checks and `git diff --check` passed. The two test files added
only for that guard are back to HEAD with no changes.

## 2026-10-19 — Backend-owned in-bank Case concurrency (superseded)

Historical checkpoint only; the frontend-owned implementation above replaces this
configuration and UI behavior. The following results describe that earlier revision.

The user requested backend configuration instead of a per-task frontend concurrency
setting. `AGENTGATE_INBANK_MAX_PARALLEL_CASES` in `.env.example` now supplies one positive
integer, default 1, for newly created in-bank ordinary, stability, and A/B Runs. Read it
before creating target descriptors or credentials; A/B reads once and pins the same
value on both manifests. Reuse the existing manifest, Engine, and grouped-session
execution runtime. No second concurrency scheduler or arbitrary 30-Case environment
limit is introduced. Invalid server configuration fails submission without creating
task resources.

The frontend hides the concurrency control for selected in-bank platform targets and
omits it from requests. External/mock/Demo/local-bank directory, token, and execution
behavior remain unchanged. The shared ordinary platform API allows the field to be
omitted in in-bank mode, but still requires it in external/mock mode. Existing in-bank
clients cannot override the backend value by sending the old request field.

Configuration is pinned when creating the Run, not reloaded by workers. Existing Runs,
reservations, and reruns keep their recorded manifests; restarting the submission
service with a changed environment affects new tasks. This is a per-Run limit, not a
global concurrency quota: concurrently scheduled A/B sides can use up to twice the
configured Case count. Same-Case turns and explicitly shared sessions remain ordered.
No customer HTTP payload, Trace parser, evaluator, database schema, worker scheduling, or
customer attachment behavior is changed. No commit or push has been performed.

Verification: full backend regression **2534 passed / 58 skipped**; frontend unit
tests **91 passed**; affected browser checks **18 passed**. The complete task-form/
directory browser file has **72 passed / 2 pre-existing baseline failures** (legacy
A/B static-analysis availability and uncovered-case confirmation), not expanded in
this change. TypeScript checks, production build, affected-file Ruff/ESLint, and
`git diff --check` passed. The build retains the existing large-chunk warnings. Tests
use local mocks; real in-bank deployment acceptance is still required.

## 2026-10-19 — Yunxia final-message output alignment

The customer original and revised `yunxia.py` both retain the last `event: message`
data in `message_data`, independently from `event: trace`. Yunxia now uses the complete
decoded message object as each turn's output and the last turn's `final_output`.
Customer fields, including nested business data, are preserved without inventing
`output`, `intent_code`, `slots`, or `workflow_calls` defaults. Non-object message data
keeps the existing `{output: value}` container required by the object-valued Trace
contract. No new business validation or public Domain schema is introduced.

Removed the redundant response copy on Turn Span attributes, the evaluator's raw
response fallback, and special JSON-string redaction. Normal output path observation
and structured Trace redaction handle the customer object directly. A customer's
`message` field is evaluated at `message`; no `output` alias is manufactured. Existing
datasets using the synthetic four-field whole output or such an alias need their
expectations adjusted. ChatABC, Trace collection, tool/Skill extraction, and database
schemas are unchanged. No commit or push has been performed.

Verification after this correction: full backend regression **2504 passed / 58 skipped**;
frontend contract/data tests **91 passed**, TypeScript checks passed, and production
build passed with the existing large-chunk warnings. Affected Python Ruff checks and
`git diff --check` passed. Coverage includes Dataset creation/publication, execution,
complete response persistence, native field-path scoring, and rerun snapshots. All
services were local mocks; real in-bank acceptance remains outstanding.

Known pre-existing limitation, reproduced without changing Domain code: an object-valued
`Equals.expected` becomes `FrozenJsonObject` and fails JSON serialization while publishing
or snapshotting a DatasetVersion. Direct whole-object Rule comparison works, but this
is not a claim that such expectations can currently be persisted end to end. Worker
regression verifies complete response persistence and scoring through scalar field paths.
The unrelated expectation serializer is unchanged in this scoped output correction.

## 2026-10-18 — In-bank execution parity (PR #57 baseline)

Scope approved for one implementation pass: failure isolation, Case concurrency,
independent timeout configuration, Yunxia customer-ID parity, and complete response
evidence. Attachments remain deferred. Work is on `feature/inbank-execution-parity`
from PR #57 HEAD `98ff389`; no commit, push, or merge has been performed.

The customer HTTP payloads, session variables, SSE parsers, creation/init retries,
and default timeouts are reused. One small in-bank runtime owns the shared thread
pool, per-session execution order, cancellation, and the overall pre-turn deadline;
both adapters consume it. Existing Run manifest concurrency is propagated rather
than adding another concurrency configuration. Local-bank and external-mode
routing/credentials remain unchanged.

Failed turns retain sanitized errors and successful turns keep their real Trace
evidence. Other turns and Cases continue as in the customer scripts. Init failure
is isolated to its Case; shared Pod preparation and cancellation remain task-level.
Evaluation uses the existing ERROR/report path with no normal score for an execution
failure, retains completed Rule checks, and skips case-level Judge calls for incomplete
conversations. Existing successful score aggregation is unchanged.

Yunxia now directly concatenates the customer's session/custom-customer ID with one
Run-scoped UUID hex, without the extra test prefix or an inserted hyphen, matching the
customer script. Multi-turn IDs stay stable and separate Runs remain isolated.
Final-response storage was simplified on 2026-10-19 as documented above; the entire
customer message object is now the output rather than a second copy on Turn Spans.

All timeouts are configured together in `.env.example`. Overall timeout defaults to
unlimited and is checked before each turn, not forcibly applied to in-flight requests.
Pod readiness is independent of the chat request timeout. No new business-field
whitelists, required customer fields, database tables, or frontend controls are added.

Verification before the final-message alignment: the complete backend suite passed
(2489 passed, 56 skipped), including
default/explicit customer-ID formatting, multi-turn stability, and cross-Run isolation. Frontend
pure contract/data tests passed (91), TypeScript checks passed, and the production
build passed with its existing large-chunk warnings. Focused execution/budget/BJS
tests passed (131). Static syntax/import/unused-reference checks and `git diff --check`
passed. Worker-entry concurrency coverage passed for all three agent modes (3 tests)
and is included in the final full regression. All services were local mocks and all databases were temporary;
real in-bank gateway and Pod acceptance is still required.

Delivery files at this checkpoint: nine backend business files (the two adapters; shared in-bank execution;
Run protocol/engine; submission/worker guards; result error classification; evaluator
execution), six test files, `.env.example`,
and these two existing progress/implementation documents. Local `.venv` and
`frontend/node_modules` links only reuse installed dependencies and are not delivery
files. No frontend business source or customer attachments were changed.

## Yunxia native request fields in turn input — 2026-10-17

- User-approved contract: Yunxia request configuration belongs entirely in each turn
  input, not Case initialization. Only nonblank `txt` is required. Optional native
  fields are `appHistory`, `custID`, `sessionId` and `config_variables`; missing history
  and variables use empty arrays, and missing identifiers retain automatic defaults.
- Reuse the customer payload builder, history normalization, execution flags and
  Run-scoped customer suffix. Explicit customer IDs do not require the test prefix.
  One Case retains one session, with an optional consistent explicit `sessionId`.
- Change only three business files: task submission checks, in-bank descriptor schema
  and the Yunxia adapter. The sample editor, domain/storage models, HTTP endpoints,
  Trace collection, scoring, attachments and external protocols are not changed.
- Focused tests cover text-only and JSON-text defaults, independent optional fields,
  full native JSON, multiple turns and Cases, exact customer payloads, stored input,
  scoring and rerun snapshots. Full backend regression: **2436 passed / 56 skipped**;
  frontend unit tests: **91 passed**; task-form/directory browser regression:
  **60 passed / 2 previously reproduced failures** (A/B static-analysis availability
  and uncovered-case confirmation). Type checking, affected-component ESLint,
  production build and diff checks passed. No real in-bank service was contacted.

## Base orchestration business JSON inputs — 2026-10-17

- User-approved extension: in-bank base orchestration now accepts the same per-turn
  business JSON objects as workflows. Both reuse one ChatABC input conversion;
  plain text and pre-encoded JSON text remain unchanged, and objects are encoded once
  into customer `chat.data.txt` without dropping fields or changing the wire protocol.
- Only three existing business files change: task-form validation, the in-bank target
  input schema, and the ChatABC adapter. Case initialization, attachment upload support,
  Yunxia/external protocols, Trace collection and scoring are not changed.
- Focused regression covers both modes, separate Case sessions, mixed text/JSON turns,
  nested values, exact customer payloads, stored inputs, pass/fail scoring and reruns.
- Validation: full backend **2422 passed / 56 skipped**, including **404 related tests**;
  frontend unit tests **91 passed**, focused browser rerun **14 passed**, type checking,
  affected-component ESLint and production build passed. The full browser run had
  **64 passed / 3 failed**: one version-selection timeout passed on rerun after source
  editing stopped; the remaining two are the previously reproduced A/B static-analysis
  and uncovered-case confirmation failures. Test environment setup restored the
  expected local virtual-environment path without changing deployment scripts.
  No real in-bank service was contacted.

## Workflow Case initialization and structured turn inputs — 2026-10-17

- Scope approved: in-bank workflows only, Case-scoped initialization and per-turn text
  or complete business JSON; no attachments, database migration or changes to other
  Agent protocols. The user explicitly waived per-file approval for this feature.
- Branch: `feature/workflow-structured-input`, based on updated `refactor-1` (`102ed29`).
  Reused the existing immutable Case model, JSON/Excel exchange, session lifecycle,
  Trace and evaluator pipeline. No old branch implementation was copied.
- The existing sample editor now edits `initial_state.config_variables`; annotations
  keep their existing non-executable meaning. The **背景知识** and **自定义变量**
  inputs have been removed from the editor at the user's request; saved extensions
  remain intact. Task submission accepts this configuration
  and complete business objects only for in-bank workflows. The adapter serializes the
  full business object into chat `txt` without extracting or dropping individual fields.
- The initialization area now uses compact Key/Value rows under **初始变量**,
  a default-collapsed section sharing the typography, color and native disclosure
  style of **其他规则条件与备注**. Collapsing retains entered values.
  The value-type selector has been removed: new values are text, while existing
  non-string values remain editable as JSON and preserve their types. Add/remove,
  read-only display and the stored protocol
  remain scoped to the existing sample editor. The Case badge and explanatory text
  have been removed; initialization is optional and blank added rows are ignored on
  save without discarding named empty-string values.
  The current UI passed all **20 sample-editor browser tests**, including optional
  initialization, matching disclosure styles, saving while collapsed and preserving
  hidden historical annotations. TypeScript, affected-component ESLint, formatting
  and diff checks passed. The local preview runs on `127.0.0.1:5198` with a separate
  temporary database.
- Local API acceptance covers dataset creation/publication, task snapshot, execution,
  persisted per-turn Trace, pass/fail scoring and rerun snapshot/history preservation.
  Customer request-contract assertions cover the exact three string initialization
  variables, complete business JSON in chat `txt`, shared envelope fields and separate
  Case sessions. Related adapter/application regression: **297 passed**.
  The new end-to-end test now compares results by Case ID instead of assuming random
  UUID result order matches creation order; five repeated runs passed.
- Validation: full backend **2408 passed / 56 skipped**; frontend unit tests **91 passed**;
  browser regression **51 passed / 2 failed**. Both browser failures (legacy A/B static
  analysis availability and uncovered-case confirmation) were reproduced unchanged on
  `102ed29`; no out-of-scope behavior or test expectations were changed to hide them.
  Type checking, affected-file ESLint, Python Ruff checks and production build passed.
  Initial BJS subprocess failures were resolved by supplying the expected local test
  virtual environment, without application changes. Real in-bank deployment is not
  claimed; see [ChatABC input instructions](inbank-evidence-acceptance.md#chatabc-business-inputs--2026-10-17).
## 行内任务创建表单精简 — 2026-10-19

行内新建单任务和 A/B 试验不再加载或展示本地 Demo 目录、目录回退提示和重复的版本选择区，
只保留平台智能体选择；未选完整平台目标时禁止提交。行外仍保留原有 Demo A/B 入口。
切换登录模式时清空旧目录选择并使未完成请求失效，避免行外响应回填行内表单。
A/B 候选版本和说明合并到同一行，窄屏按顺序堆叠，收紧移除 Demo 区后的留白。
验证：组件 ESLint、类型检查、生产构建及 20 项相关隔离浏览器场景通过，
覆盖行内平台提交、行外 Demo、登录模式切换及迟到响应；已检查桌面和 600px 窄屏截图。

## 本地集成代码快照 — 2026-10-17

按用户要求，从当前本地代码创建 `integration/baibo1017`，整合评估器、测评集、测评任务
及界面调整，同时保留示例、测试与文档。上传范围遵循 Git 忽略规则，不包含密钥、本地配置、
数据库、运行日志及依赖目录。
上传前验证：后端 2501 passed / 83 skipped，前端单元测试 110 passed，生产构建通过。
环境相关集成测试按配置跳过；静态敏感信息扫描及新增示例截图检查未发现真实凭据。

## 标注入口调整 — 2026-10-17

左侧“标注”移到“测评任务”下方。标注页面展示“待实现”，不再挂载原标注工作台。
相关组件 ESLint、类型检查、生产构建和差异格式检查通过。

## 任务详情移除耗时列 — 2026-10-17

执行结果与低分样本表格移除“耗时”列，并同步收紧基础列宽，保留 10 行高度及固定操作列。
组件 ESLint、格式检查及对应隔离浏览器回归通过。

## 执行结果表格高度 — 2026-10-17

按用户反馈，将结果表格最大高度统一增至 860px，可容纳表头与约 10 行样本，
移除随视口高度压缩至三四行的限制。少量样本继续按实际内容高度展示；
超出部分保留内部滚动、固定表头和操作列。
组件 ESLint、格式检查及对应浏览器回归通过；桌面和窄屏均可完整容纳至少 10 行，
第 12 行及右侧评分列仍可滚动查看。

## 多评估器执行结果显示修复 — 2026-10-17

复现“测试2”的 12 条样本、6 个评估器结果：原表格被列最小宽度和不换行表头撑至约 1573px，
横向滚动条位于长表底部。改为按列分配宽度，评估器表头允许换行，长样本名称和场景
单行省略并提供悬停全文；结果区域独立滚动，固定表头与右侧操作列。
1265px 桌面视口可完整展示六项评估器列；窄屏保留局部横向滚动，不撑宽整页。
执行结果与低分样本共用修复，不改变评分、样本数据或行内/行外 Agent 路由。
验证：类型检查、生产构建、组件 ESLint 和格式检查通过；5 项相关隔离浏览器场景通过，
覆盖多评估器布局、末条样本可达、固定列、低分筛选及原页签内容。
使用“测试2”已有结果在隔离环境回放，并检查 1440px、1265px、768px 视口截图，未重新执行任务。

## 执行结果概览文字 — 2026-10-16

统计主数值由 28px 调为 24px，辅助数值由 16px 调为 14px，统一采用常规字重。
失败计数改用独立颜色类，去掉通用危险按钮样式带来的红框、背景及内边距，保留红色数字。
相关组件 ESLint、Prettier 和差异格式检查通过。
按后续要求，主数值进一步缩至 20px，辅助数值缩至 13px，移除概览完成进度条，
保留百分比文本；样本详情中的得分进度条保持原样。

## 任务详情卡片高度 — 2026-10-16

收紧任务信息卡与六项统计卡的上下留白、图标及统计项间距，保留原字号和横向布局。
1440px 视口下，信息卡由 139.19px 降至 91.59px，统计卡由 154.59px 降至 102.59px，
均约为原高度的三分之二。已检查桌面及 768px 窄屏截图，无内容裁切；组件 ESLint 通过。

## 测评任务详情页签 — 2026-10-16

“样本结果”更名为“执行结果”，依次新增“低分样本”“优化迭代”；移除“Skill 静态分析”页签，
原“任务配置”更名为“任务快照”并放到最后。低分样本沿用低于 80 分的筛选口径，
未评分不计入，支持筛选、导出和详情查看；详情上下条及返回保留低分范围。
优化迭代按用户要求统一展示「能力构建中」，移除本页报告分析内容与按钮，不触发模型请求。
验证：lint、类型检查、生产构建、9 项任务报告单元测试及 4 项隔离浏览器测试通过；
覆盖边界分数、导出、详情返回、快照以及优化分析调用和空态，未调用真实模型。
后续占位内容调整通过类型检查、相关组件 ESLint 和 3 项浏览器回归，覆盖已完成、运行中、
失败任务统一展示「能力构建中」，并确认不请求模型分析。

## 预置模板创建按钮 — 2026-10-16

新建窗口中的模板按钮由“应用”改为“使用此模版创建”，与右上角“自定义创建 LLM 评估器”
共用绿色主按钮样式。首页继续只读查看，两处模板卡片宽高保持一致。
lint、类型检查、生产构建和 3 项相关浏览器场景通过，实测两种创建按钮的尺寸及样式一致，
模板应用、自定义创建与保存发布正常；已检查模板窗口截图。

## 精简新建评估器页顶部 — 2026-10-16

新建 LLM 评估器不再展示图标、标题、状态、备注预览、日期和收起基本信息的顶部概览，
名称与备注输入框直接展示，并移除概览下方分隔线及留白。已有评估器的详情仍展示元信息。
lint、类型检查、生产构建及 3 项现有浏览器场景通过，覆盖模板应用、自定义新建和保存发布；
已检查基本信息截图。

## Prompt 变量提示实时更新 — 2026-10-16

综合评估器子项与历史单体编辑器的变量提示改为从当前 System/User Prompt 实时提取，
按首次出现顺序合并去重、去除名称首尾空格；空名称不显示，无变量时隐藏整行。
手动删除、清空按钮、移除 User Prompt 和切换子项均同步更新；input/output 保留原含义说明。
其他名称只反映当前文本，执行变量支持与原有保存校验不变。
验证：lint、类型检查、生产构建、15 项相关单元测试及 10 项隔离浏览器回归通过，
覆盖动态增删、System/User 去重、空括号隐藏及子项间切换，原有保存发布行为正常。

## LLM 评估器卡片与基本信息样式对齐 — 2026-10-16

父评估器备注改为与名称相同高度的单行输入框，保留 200 字限制及保存行为。
综合模板卡片复用规则卡的白底、浅绿标签、深色标题、边框、悬停效果和底部文字操作，
首页与新建弹窗统一 320px 列宽及内容排版，小屏幕按可用宽度收缩。
首页仍为只读查看，新建仍为应用模板；四子项和权重不变。
验证：lint、类型检查、生产构建及 10 项隔离浏览器回归通过，实测两处卡片宽高一致、
名称与备注框等高，已检查首页、模板弹窗和基本信息截图。

## 清理已有 LLM 评估器实例 — 2026-10-16

按用户明确指出的目录范围，备份本机数据库后删除 7 个已保存的 LLM 评估器实例，
包括原子评估器端到端示例、三个简洁性/一致性实例和三个贷款 Agent 的核心质量 LLM。
本机 LLM 首页现在仅保留“回答质量综合评估”只读模板；新建后的自建评估器仍可正常显示。
删除通过服务端归档并停用实现，不按名称或 ID 在前端隐藏；归档项禁止再修改、发布或选入新任务。
精确发布版本、原任务及引用保留，清理前后整表指纹一致。API、Worker、前端、目录、
被测 Agent 和 Trace 服务已重启并检查可用。示例源码与历史执行证据继续保留。

验证：Python 全量 **2501 passed / 83 skipped**，10 项隔离浏览器测试通过，
覆盖 7 个实例删除后刷新只剩模板、旧地址刷新返回目录、历史版本读取及既有创建发布流程。
存储约束和归档后禁止写入的测试通过；未配置的 MySQL 等集成测试跳过，未声明实测。

## LLM 综合模板首页只读 — 2026-10-16

仅保留“回答质量综合评估”预置模板，四个子项继续作为模板内部配置。
移除子评估器及历史单体编辑页的独立模板选择。首页模板卡片只提供“查看详情”，
弹窗中可切换只读查看四子权重及提示词，不创建配置。应用模板保留在新建 LLM 评估器流程，
后续配置、删增、保存发布能力不变。沿用深绿色名称背景；未修改已有自建评估器或历史版本。
验证：110 项前端单元测试、9 项隔离浏览器测试、lint、类型检查及生产构建通过；
只读详情切换四子项不产生 API 写入，新建应用及保存发布流程正常。

## 预置 LLM 列表与模板样式 — 2026-10-16

LLM 评估列表中的旧内置单体卡片替换为 **回答质量综合评估** 模板；点击应用直接进入四子评估器
编辑表单。列表与新建模板弹窗共用卡片，名称区域采用主题深绿色背景及白字，展示既有
简洁性 10%、相关性 30%、准确性 40%、合理性 20% 配置。规则、自建和历史读取路径不变，
模型仍在应用后配置，保存发布后用于测评任务。
验证：110 项前端单元测试、9 项隔离浏览器测试、lint、类型检查及生产构建通过。
已检查目录和新建弹窗截图，深绿色标题及四子项权重显示一致。

## 综合评估器模板入口修正 — 2026-10-16

新建 LLM 评估器现在先展示 **回答质量综合评估** 模板，包含简洁性、相关性、准确性、合理性
四个子评估器，默认权重依次为 10%、30%、40%、20%。应用后可分别配置；左侧每行提供删除按钮，
“新增”直接创建并选中空白子项，Prompt 模板入口仍可按需选择预置子模板。
删除非当前子项时保留当前编辑选择；新建模板弹窗整体增高约三分之一，并适配小屏幕视口。
复用现有表单和整组保存/发布，无后端契约改动，已有评估器实例不变。
验证：前端 110 项单元测试、8 项隔离浏览器测试、lint、类型检查及生产构建全部通过。
在 1440×1000 视口实测模板面板高 839.33px（原约 630px），完整位于屏幕内。

## LLM 子评估器 — 2026-10-16

用户确认 UI 并明确授权豁免逐文件确认；在 `feature/llm-child-evaluators` 完成实现，
未提交、推送或合并。分支基于同步后的 `origin/refactor-1`（`102ed29`），保留既有未提交修改。

新建 LLM 评估器支持父级名称、备注、状态、版本、创建/更新日期；左侧管理子评估器及权重，
右侧独立配置模型、System/User Prompt、模板及阈值。预置相关性、准确性、合理性、简洁性；
旧单体和单次请求多维度评估器继续打开原编辑器。按钮沿用绿色主题。

新增 `llm_group@1`，整组保存、发布和草稿清理采用 SQLite/MySQL 事务及 CAS；
禁止子项独立修改/选用/跨组借用，并防止旧通用接口绕过整组版本校验。
父版本固定子版本、权重和版本化备注/模板来源；各子项独立调用模型和保存 JudgeRecord，
父项用十进制加权并只计入任务总分一次。单任务、A/B、稳定性及行内/行外入口均有精确引用测试；
本次没有改动被测 Agent 数据源与 Token 路由。

真实 DeepSeek 官方 `deepseek-v4-pro` 三子项调用完成，保留评估器
`e9d657db-d59c-4c00-b742-8dcdbcce706b` v1 和任务
`7a9d8589-80b0-471d-9fc4-da540c8f96ad`。模拟 Agent 回显问题，三项均得 0，
综合分为 0；模型执行、证据留存及聚合验证通过，低分仍如实显示不通过。
重启完整本地服务后再次只读验证持久化结果成功。复现、摘要及说明见
[子评估器示例](../examples/llm-group/README.md)。密钥仅由已有本机配置提供。

最终验证：Python 全量 **2483 passed / 78 skipped**（未配置 MySQL、Redis Cluster、
私有客户归档，以及不适用于所选适配器的用例）；前端 **108** 项单元测试、栈检查、lint、
类型检查和生产构建通过；**8** 项独立临时数据库浏览器测试覆盖模板、多子配置、保存失败、
原子发布、历史版本、旧编辑器和实际报告的父子展示；示例离线验证 **4** 项通过。
真实行内网关和 MySQL 集成未在本机实测，既有依赖弃用及构建包体积提示仍存在。


## LLM Prompt 编辑器与简洁性模板 — 2026-10-15

在 `feature/llm-evaluator-templates` 分支完成新建 LLM 评估器完整页面：名称、描述、模型、System/User Prompt、选择模版弹窗、搜索筛选和应用。内置用户提供的完整“简洁性”提示词，复用已有 Catalog 草稿、发布版本和任务引用；既有多维度评估器继续使用原编辑器。没有提交、合并或推送本次改动。

整体评分实现增加显式模板渲染，脱敏、限长后一次性替换输入输出，历史配置保持原提示词语义。DeepSeek 连接支持显式 token 字段与 thinking 设置，流式读取响应体时共享超时预算，避免空白保活无限延长请求。API Key 仅在本机忽略文件中保存。

浏览器实际验证新建、应用模板、选择模型、保存发布、刷新恢复、历史版本和任务勾选，并补齐跨组件离开和登出的未保存保护。“简洁性 v1”及延长评审超时的 v2、实际测评任务均已保留。真实 Pro 评审在 180 秒后超时，延长后的首次任务又触发原 300 秒任务回收期限；示例已对齐任务和模型预算。随后确认 [DeepSeek 官方 API 故障](https://status.deepseek.com/incidents/7116348214287)同时影响 Pro 与 Flash，结束最后一次等待，因此**真实评分端到端验收尚未完成**，没有将失败标记为通过。复现脚本、数据集、评估器配置、截图与真实结果见 [简洁性示例](../examples/llm-conciseness/README.md)。

后端全量回归 2434 项通过、56 项跳过（含沙箱限制放行后的 20 项重跑）；前端 96 项单元测试、类型检查、lint、生产构建通过；示例离线测试 4 项通过。仍有已有的依赖弃用及构建包体积提示。

## LLM 评估器保存、发布与按版本运行 — 2026-10-08

用户批准并授权完成后推送 GitHub。当前实际入口 JudgeSystem 已接通服务端草稿、发布启用与版本历史；
保存后刷新可恢复完整维度配置，保存失败保留编辑内容，写成功而刷新失败时保留已完成操作的状态。
删除原前端临时草稿/历史状态。发布后旧版本保持不变，任务配置区显示当时的模型、维度提示词、权重与阈值。

新增 `answer_quality@2`，单次模型请求返回逐维度分数、置信度、依据和复核标记；后端用十进制算法
计算加权总分，前端同步严格校验权重合计。保留原 `answer_quality@1`，历史按其原协议运行。
非法响应最多纠正一次，超时、错误和纠正均保留实际请求/响应证据，不能变为通过。
复用 Catalog/SQLite/RunManifest，没有新增持久化表或迁移别名。

单任务、平台 A/B 和稳定性任务均贯通精确版本引用，行内真实 Token、行外 local Token 与目录分流保持不变。
复合评估器子项仅作为执行依赖，主评估器来自用户明确选择，避免重复参与总分计算。
基线参考与职责记录在 [Catalog 实施计划](evaluator/catalog-implementation-plan.md)，
用户验收步骤见 [LLM 持久化操作指南](evaluator/llm-persistence-guide.md)。

后端全量回归：1550 项通过、40 项跳过（两条既有依赖弃用提示）。
前端验收：88 项单元测试、6 项真实后端保存发布浏览器测试、5 项任务版本及登录分流浏览器测试通过；
栈检查、lint、类型检查与生产构建通过。浏览器使用独立临时 SQLite，模型执行使用确定性客户端验证；
本次未访问付费模型或真实行内网关。

## In-bank complete evidence and BJS dispatch integrity — 2026-10-08

- Autonomous Goal scoped to complete in-bank Trace retrieval and truthful BJS failures. User explicitly deferred in-bank Skill static analysis because no real version-definition API is available; the existing unavailable UI remains unchanged.
- ChatABC base/workflow and Yunxia now require per-turn SSE Trace references, fetch configured project evidence and model attachments, validate session/Pod/input/output/completeness, and reuse SDK normalization. Missing evidence fails instead of becoming output-only success. Query credentials remain separate from login credentials; no conversation replay on query failure. Yunxia requests debugTrace.
- Removed BJS transport-error simulated success. Tests verify waiting/retry/exhausted-failure transitions and no retry inside the dispatcher. Remote exactly-once acceptance/cancellation is not claimed.
- Validation: full backend 1469 passed/40 skipped, including positive/negative tool-rule checks and Case input aliases (28 evidence tests total); loan runtime 32 passed; unchanged frontend 82 tests, lint, typecheck/build passed using the identical frontend source in the existing dependency worktree. Seven pre-existing Ruff diagnostics in the legacy adapters/execution tests remain; new evidence and changed BJS/query files pass Ruff. Existing dependency and frontend chunk-size warnings remain.
- Verified real local HTTP Trace query transport with fixture gateways, not a real in-bank deployment. See [configuration and acceptance](inbank-evidence-acceptance.md), [implementation ownership](inbank-evidence-implementation.md).
- Restarted the full local acceptance stack with the current source and preserved/backed up the existing database. All nine external loan smoke runs (three agents × rule/LLM/hybrid) completed with passing evaluation results, numeric scores and remote Trace evidence; model records identify DeepSeek v4 Pro. Real in-bank integration remains unverified. After autonomous implementation concluded, the user explicitly authorized committing and merging/pushing this work to `origin/integration/baibo`.

## Six-target HTTP Trace reporting — 2026-10-08

- Added authenticated complete-bundle upload and independent receiver storage without modifying original vendor sources. The three loan targets transmit actual SDK events and LLM attachments; three protocol peers transmit explicitly simulated traces, including simulated failure status.
- Local full startup enables strict reported evidence and shared private reporting credentials; in-bank gateway/Trace query configuration stays separate. The UI labels simulated traces. Remote sender endpoints require HTTPS; physical cross-machine/TLS deployment is documented, not claimed as tested.
- Six tasks/nine turns passed real HTTP acceptance (three actual DeepSeek v4 Pro loan executions). Receiver queries still returned all nine traces and nine LLM attachments while sender evidence was temporarily moved aside. Files were restored.
- Verification:1398 backend tests passed,35 skipped;32 tested-Agent tests;82 frontend unit tests;lint/typecheck/build passed. See [protocol, deployment and run IDs](trace/trace-reporting.md). Current feature work is on `feature/trace-reporting`; no merge or remote push is included in this Goal.

## Portable local delivery — 2026-10-08

- Completed the local eight-process launcher with the bundled upstream Trace Server file backend and local Agent directory. Unified Redis6397 and persistent runtime paths; retained BJS scheduling and configured in-bank gateway/Trace routing.
- Shipped seven loan evaluators using the recipient's model, alongside the existing three v3 datasets. Added read-only v2 historical results/report and a command that creates nine fresh rule/LLM/hybrid tasks.
- Replaced stale installation instructions. Removed automatic global process termination from service-start commands; pre-existing services are preserved.
- Verified clean-directory installation, all nine real DeepSeek v4 Pro smoke runs, browser Trace/LLM scores, shutdown/restart persistence, and wheel seed resources. Two LLM-only tasks scored85 below the existing95 task gate; evidence is retained, not converted to pass.
- Validation:1385 backend tests passed (35 skipped),29 tested-Agent tests,82 frontend tests,lint/typecheck/build. See [delivery evidence](bank-agents/delivery-verification-20261008.md).

## Annotation v2 per-turn editor — 2026-09-26

- Enabled per-turn forms for the template's selected annotation objects. Dataset review shows original input, actual output and source expectations beside compact dimension scores, tags, a single-line comment and a human expected-answer field.
- Added independent v2 object records with local persistence, range validation, draft/completed status and unsaved-change protection. Original datasets, runs and automatic evaluation evidence remain unchanged.
- Verification: 53 frontend unit tests, typecheck/build and affected-file ESLint passed; browser checks covered selected objects, multiple turns, range rejection, save/reopen and completed state. See [implementation record](annotation/implementation-plan.md).

## Task and dataset names — 2026-09-26

- Task creation starts with a required name. Names persist with platform tasks and appear in task lists, result headings and v2 annotation task filters. Visible historical tasks are named once using Agent name plus the original Shanghai-calendar month/day (for example 924).
- Dataset creation starts with its existing name field; cards keep using stored dataset names and historical dataset names are unchanged.
- Verification: 1333 backend tests passed, 35 environment-dependent skips; 50 frontend unit tests, typecheck, build and affected-file ESLint passed. Browser-created named task completed and displayed its configured name in the list and result page. See [implementation details](task-names/implementation.md).

## Annotation v2 template and conversation list — 2026-09-26

- Added independent v2 template creation with three optional annotation groups, default 0–100 range, and separate details/start actions on cards. Existing v1 behavior is retained.
- Added a dedicated v2 conversation list with evaluation task, aggregate score, evaluation time, content-based column filters, score interval and Bad Case shortcut. Exemption is browser-persisted and reversible.
- The user confirmed this stage covers the list, filters and exemption; the three v2 scoring forms remain a later stage. Loading retains the existing limit of 200 completed runs.
- Verification: 50 frontend unit tests, typecheck, build and affected-file ESLint passed. Browser checks covered 36 conversations, 17 Bad Cases, score ranges, combined filters, pagination, conversation details and exemption reload/recovery. See [implementation plan](annotation/implementation-plan.md).

## Human annotation feedback loop — 2026-09-24

- Connected annotation templates to the existing login-aware platform directory picker; target matching pins environment, team, Agent, branch and version. Added a completed-run source entry for historical Demo/tool traces.
- Added per-tool-span scores, tags, comments, explicit call/argument expectations and completeness checks. Inputs and actual Trace evidence remain unchanged; human scores are audit notes, while explicit expectations become existing rule contracts.
- Added previewed export to a new dataset draft and writeback to the original dataset draft. Existing checks at unrelated paths, other cases, published versions and historical reports are preserved. Deleted/changed source cases and changed preview drafts require manual reconciliation. Writes use current dataset APIs; multi-user atomic concurrency is not claimed.
- Resolved template-library refresh inconsistency by deriving the library from persisted task snapshots; fixed its route display. Annotation persistence remains browser-local, with no cross-browser/team synchronization claim.
- Browser acceptance completed on 5197: external Cloudshrimp branch/version selection; message/tool score validation; refresh recovery; export and publish; Demo A/B automatic grading (risky output fails, fixed version passes Final Output/Required Tool/Tool Arguments); follow-up annotation/writeback, published v1 hash unchanged. Restarted stopped Redis/Worker/Scheduler so actual jobs complete.
- Verification: 1327 Python tests passed, 35 environment-dependent skips, two dependency warnings; 44 frontend unit tests passed; frontend typecheck/build and affected-file ESLint passed with existing bundle-size warning.
- See [end-to-end operation guide](annotation/end-to-end-guide.md) and [implementation plan](annotation/implementation-plan.md). Changes remain uncommitted on `feature/annotation-feedback`; pre-existing task-deletion edits are preserved.

## Agent platform local acceptance — 2026-09-22

- Requirement 001 is implemented end to end on `feature/agent-target-selection`; the user's final instruction waived remaining approval checkpoints. Local UI, directory mock, exact target snapshot, encrypted credentials, real task persistence, worker execution, scheduling and result pages are connected.
- Workflow retains agentId + agentVersion. Cloudshrimp branch creation is explicitly mock-only. Dataset/execution selections survive target changes; missing graph/static-analysis metadata is explained in the UI.
- Live workflow, base reservation and Cloudshrimp stability tasks completed successfully. Original Demo A/B also completed and displayed comparison results. Fixed the form's unsupported A/B parameters and single-task detail identity coupling.
- Verification: 1237 Python tests passed, 25 environment-dependent skips, two dependency warnings; 36 focused browser tests passed; frontend typecheck/build passed with the existing bundle-size warning.
- Ready at http://127.0.0.1:5199/#/tasks . See [local acceptance instructions](../script/agent-platform-mock/README.md) and [completed implementation record](agent-platform/implementation-plan.md). No commit, push or merge performed. Earlier checkpoints below are historical.

## Agent platform submission route — 2026-09-21

- Implemented the approved HTTP route in isolation: strict target/task input checks, separate transient token header, explicit application callable, caller/team separation, safe errors and the agreed 202 task/run response.
- Verification: 84 new route scenarios and 44 existing related regressions passed (128 total); Ruff lint/format checks passed. Existing dependency deprecation warnings remain. Tests use fake submitters and verify response-envelope integration without real platform calls.
- Route registration and production application submission remain pending. No task persistence, runtime execution, local peer or proxy is claimed. No commit, push or merge performed.

## Agent platform task form — 2026-09-21

- User approved the implemented and verified form checkpoint. The responsibility of `src/agentgate/server/routes/agent_platform.py` is also approved. Its detailed design is approved and isolated implementation is complete; see the newer route checkpoint above.

- Implemented the approved `EvaluationTaskForm.vue` design: the single-task path now uses the platform picker/provider; platform target changes preserve dataset/evaluator/execution settings and source-prefilled cases. A/B retains its existing endpoint and association behavior, including Demo availability when the registered bank catalog fails.
- Submission takes one target/token/form snapshot, locks controls before asynchronous validation, sends the token only in a request-specific header and validates the returned task/run association. Refresh failure preserves confirmed task identity; uncertain creation is reported without retries or raw server detail.
- Verification: 36 focused browser scenarios passed across the complete 35-test run and the added A/B catalog-failure regression; affected A/B tests were rerun after that compatibility adjustment. Frontend typecheck/build, lint and formatting passed, with the existing large-bundle warning. Tests intercept HTTP locally and do not demonstrate backend execution.
- Backend task creation, proxy, local peer and execution loop remain pending their per-file reviews. New-platform static analysis/graph are explicitly unavailable until matching metadata exists. Workflow instance creation remains `agentId + agentVersion` with taskId. No commit, push or merge performed.

## Agent platform directory API — 2026-09-21

- Implemented the approved `frontend/src/api/agent-platform.ts` provider for interfaces 2–6, with per-call tokens, separate platform origins, complete pagination, explicit type normalization, branch-version consistency checks and sanitized failures. Existing AgentGate Axios requests are unchanged.
- Verification: 23 browser tests passed (11 new HTTP/provider tests and 12 picker regression tests); frontend typecheck/build, API lint and formatting passed. The picker/provider integration test intercepts HTTP locally and does not claim real bank execution.
- At this API checkpoint, production form wiring, proxy configuration, local peer and task execution remained pending. The newer form checkpoint above supersedes the frontend wiring status. No commit, push or merge performed.

## Agent target picker component — 2026-09-21

- On `feature/agent-target-selection`, implemented the approved isolated selection component with an explicit directory input: temporary token login/logout, team/type/agent/branch/version selection, exact-target submission reads and stale-response rejection.
- Verification: 12 focused browser tests passed; frontend typecheck/build, component lint and formatting passed. Existing large-bundle warning remains.
- At that component checkpoint the production task form, network provider, local peer and execution integration remained pending their file reviews; A/B and dataset/execution controls have not been modified. See [implementation plan](agent-platform/implementation-plan.md) and [requirement 001](requirements/001-agent-target-selection.md).

## Upstream 33db48a integration — 2026-09-18

- Integrated the new open-fin-sub/agentgate refactor-1 backend into an isolated delivery copy, retaining the existing UI, local-bank adapter, SDK, task links and UUID redaction fix.
- Added safe legacy table-prefix/identity migration; verified all 16 old tables and 112 historical runs on a read-only-source SQLite backup.
- Adapted task/sample/stability visibility and effective concurrency snapshots. Explicitly reject unsupported raw per-run API keys and nonfunctional BJS dispatch. Retain explicit external model environment loading.
- Backend: 1033 passed / 1 skipped; tested agents: 19 passed; frontend build passed. Three live browser runs covered 24 cases / 27 turns, with 297 raw SDK/database/API evidence checks passing. Live Judge, composite, static analysis and root-cause reports also persisted successfully; Judge review outcomes remain unchanged.
- Full checkpoint, delivery upgrade instructions and customer factory limitations: workspace-root docs/bank-agents/upstream-sync-20260918.md. Customer Pod create/readiness/upload/delete are not wired into the current local adapter and remain unverified in the customer environment.

## Reproducible bank-target delivery — 2026-09-17

- Repository-local Trace SDK dependency, separate locked Python environments, automatic 24-case database initialization, and fresh-run browser/Trace acceptance scripts are included.
- Current handoff instructions and validation are in workspace-root docs/bank-agents/. Older checkpoint references to developer-local audit documents below are historical records, not prerequisites for installing this delivery.
- Customer factory lifecycle, full cloudshrimp protocol and production equivalence remain outside the verified scope.

## Trace API correlation-ID redaction fix — 2026-09-17

- Preserve full UUIDs only in request/session/trace/span correlation fields (including namespaced keys); explicit sensitive-key policies still take priority. Other values still undergo recursive credential/PII redaction.
- Reproduced the historical Luhn/UUID collision before the fix. Added 29 regression cases including the real UUID, nested attributes, sensitive values in ID fields, explicit policy overrides, and an HTTP route/storage-invariance test.
- Verification: focused suite 83 passed / 1 skipped; full backend 1023 passed / 1 skipped (private SDK fixture), with two existing dependency deprecation warnings.
- Restarted the idle local stack. Across the existing three conformance runs, all 27 request IDs now match canonical storage; all 24 canonical Trace payloads and 27 raw JSONL file digests remain unchanged. No frontend, tested-Agent or original evidence changes; no model rerun.
- Evidence: workspace-root BANK-CONFORMANCE-AUDIT-20260917.md, follow-up section; runtime/conformance-20260917/trace-api-redaction-fix.json. Original audit evidence is retained.

## Full-stack real-target integration — 2026-09-17

- Existing UI layout retained; creation now binds the three live targets, published database cases, capability limits and exact descriptor fingerprints. Static analysis no longer routes these targets through Demo or aliases unrelated v1 descriptors.
- Added safe model metadata, pinned descriptor lookup, partial samples for non-completed runs, real-target stability and scheduled launches; one supervisor can launch all six local processes.
- Fixed SDK UUID/phone masking collision through a narrow public SDK rule override, retaining actual phone masking. Judge invalid JSON values get at most one correction; both responses/fingerprints persist and invalid results remain errors.
- Live tests include actual model execution, composite propagation, static report/reanalysis, scheduled dispatch, stability rounds, notes writeback and a versioned negative-control regression. Real model business failures and an optimizer invalid-confidence edge case remain visible, not coerced into passes.
- Workspace-root `FULLSTACK-ACCEPTANCE-20260917.md` supersedes older frontend-not-bound checkpoints below and records exact run IDs and production boundaries.
- Final verification: 994 backend tests passed / 1 skipped; 19 tested-Agent tests passed; 16 unified UI tests and 4 new target UI tests passed; production frontend build passed. All three modes completed a final live low-risk smoke run after restart. Services remain local and running.

## Independent bank-tested Agent checkpoint — 2026-09-17

- Added a separate `tested-agents/` service: real model tool-loop, LangGraph workflow, and LangGraph Skill-router/cloudshrimp modes. Shares synthetic SQLite loan tools and the supplied actual Trace SDK, not replayed model answers.
- Added loopback HTTP Target adapter, `/api/bank-targets`, `/api/bank-evaluations`, and Celery dispatch. Pinned descriptors include code hash; request/session/Trace output are cross-checked. Actual turn inputs come from SDK evidence.
- Added three persisted eight-case datasets. Full live-model runs cover low/high risk, rejection, amount boundary, over-limit review, missing fields, multi-turn completion and status query.
- Verification: 18 independent-service tests and 991 backend tests including the private SDK fixture. Live results and exact run IDs are in workspace-root `BANK-TESTED-AGENTS-20260917.md`.
- Frontend source/layout unchanged in this checkpoint; existing report pages render these tasks, but create-form target selection and global Demo wording are not yet rebound.
- This implements a reference-compatible local tested Agent, not the bank's undisclosed production internals or remote factory lifecycle. See `tested-agents/README.md` for unsupported protocol fields and operational limits.

## Customer SDK integration checkpoint — 2026-09-17

- Added strict customer SDK JSONL normalization and a hash-pinned replay Target adapter using the unchanged refactor-1 runtime/Trace contracts (upstream e3760d1).
- Added explicit ChatABC/cloudshrimp request and SSE protocol translation; no customer HTTP calls or resource operations are wired yet.
- Verified the supplied private city-research archive through RunManagement, RunEngine, existing rule evaluators, and isolated SQLite. The archive is not a loan-agent acceptance test and is not copied into this repository.
- Verification: 29 focused tests including the private archive; 982 full backend tests passed. Frontend unchanged in this checkpoint.
- Still pending: exact customer loan Target/version, request-to-SDK Trace correlation, live HTTP adapter, worker/catalog composition, resource lifecycle ownership, and UI data binding. See workspace-root `CUSTOMER-AGENT-API-CONTRACT-20260917.md`.

## Local UX integration checkpoint — 2026-09-17

These changes are in the local unified-task integration worktree, not a claim of an upstream merge.

- Task identities and run/static-report associations are persisted in SQLite. Single, A/B, and stability launches save runs and their task associations atomically before dispatch.
- Stability launches support 2–20 independent runs with an identical manifest. Summary excludes failed/error/unscored runs from numerical statistics and preserves every round's status.
- Optimization reports are persisted by evidence/model/analyzer fingerprint. Analyzer v3 restricts model citations to the same Span allowlist enforced by the report domain and uses representative evidence with one bounded contract retry.
- Registered composite v1 supports all, any, and weighted scores while preserving blocking failures, errors, and review outcomes.
- Frontend task relations use server data. Reanalysis replaces the active task-to-static-report link without deleting older reports. Existing layout and the three-column tuning workbench remain.
- Verification: 953 backend tests, frontend production build, and the unified-task browser suite. Exact live run IDs and remaining boundaries are recorded in the workspace-root `INTEGRATION-20260917.md`.
- Model connection editing/team authorization still requires a choice between local single-user management and a multi-user identity/authorization system. Existing service-managed model execution is verified; there is no claim of production tenant isolation.

## Status Legend

- `[x]`: implemented and covered by the current test suite.
- `[ ]`: not implemented or not yet accepted as complete.
- Paths marked **new** do not exist yet.
- This checklist tracks the complete POC direction. Deferred production work is
  listed separately and is not required to finish the initial demo.

## Work Ownership

| Tag | Scope | Status |
|---|---|---|
| `[CODEX-EVALUATOR]` | Persistent Evaluator Catalog | Complete and integrated into `refactor-1` |
| `[CODEX-SKILL]` | Static Skill Analysis backend and API | Complete and integrated into `refactor-1` |
| `[CODEX-OPTIMIZER]` | LLM root-cause optimizer backend and API | Implemented and verified on `feature/llm-root-cause-analysis`; delivery pending |
| `[CODEX-SCHEDULE]` | One-time scheduled Evaluation Runs | Complete and uncommitted on `feature/scheduled-runs` |
| `[UNASSIGNED]` | Web pages | Not started |

## Core Foundation

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Domain models | Define Dataset, Case, Run, Trace, Result, Target, and Evaluator concepts | `src/agentgate/domain/` |
| [x] | SQLite storage | Persist the POC domain objects | `src/agentgate/storage/sqlite.py` |
| [x] | Repository contract | Isolate application workflows from storage implementations | `src/agentgate/storage/repository.py` |
| [ ] | Artifact storage | Store files, screenshots, reports, and generated outputs | `src/agentgate/storage/artifacts.py` **new** |
| [x] | Storage cleanup | Remove obsolete or empty storage code after migration | `src/agentgate/storage/` |

## Dataset

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Dataset management | Create, edit, archive, publish, and version Datasets | `src/agentgate/application/dataset_management.py` |
| [x] | Dataset loading | Convert external data into Dataset and Case models | `src/agentgate/dataset/loader.py` |
| [x] | JSON format | Import and export complete Dataset structures | `src/agentgate/dataset/formats/json.py` |
| [x] | Excel format | Import existing single-sheet customer files | `src/agentgate/dataset/formats/xlsx.py` |
| [x] | Multi-turn Cases | Store multiple conversation turns in one Case | `src/agentgate/domain/case.py` |
| [ ] | Dataset sampling | Select reproducible smoke, regression, tagged, or risk-based subsets | `src/agentgate/dataset/sampling.py` **new**; planned after 2026-09-15 |
| [ ] | Dataset generation | Generate positive, negative, and boundary Cases from Agent metadata | `src/agentgate/dataset/generation/` **new**; planned after 2026-09-15 |
| [ ] | Public benchmarks | Import selected public evaluation datasets | `src/agentgate/dataset/benchmarks/` **new**; planned after 2026-09-15 |

## Evaluator And Result

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Rule evaluation | Evaluate routing, Tool use, state, policy, and output | `src/agentgate/evaluator/rule/` |
| [x] | Metrics | Aggregate Case Results into Run metrics | `src/agentgate/result/metrics.py` |
| [x] | Release gate | Decide whether a version passes evaluation | `src/agentgate/result/gate.py` |
| [x] | Report | Build the complete evaluation report | `src/agentgate/result/report.py` |
| [x] | Evaluator structure | Separate protocol, executor, runtime models, and Rule responsibilities | `src/agentgate/evaluator/` |
| [x] | JSON Schema Rule evaluation | Validate structured values with Draft 2020-12 structure, required-field, value, and composition keywords; allow safe local JSON Pointers; and reject invalid schemas during Run preflight for output, state, routing, and Tool-argument expectations | `src/agentgate/evaluator/rule/json_schema.py`, `src/agentgate/evaluator/rule/operators.py`, `src/agentgate/application/evaluator_management.py` |
| [x] | LLM Judge | Perform redacted case-level semantic answer-quality evaluation through configured models | `src/agentgate/evaluator/judge/` |
| [x] | OpenAI-compatible model transport | Call preconfigured public or private Chat Completions endpoints using resolved credentials | `src/agentgate/integrations/model_providers/` |
| [x] | POC Judge environment configuration | Build one optional process-level model connection from four environment variables, remain Rule-only when absent, and reject partial configuration | `src/agentgate/integrations/model_providers/environment.py` |
| [ ] | Persistent model provider configuration | Store allowlisted endpoints, managed secrets, and production credential resolution for application use | Design required before implementation |
| [ ] | Multimodal evaluation | Evaluate files, images, and other Artifacts | `src/agentgate/evaluator/judge/multimodal.py` **new**; planned after 2026-09-15 |
| [x] | Result comparison | Compare two compatible EvaluationRuns and expose the comparison API | `src/agentgate/result/comparison.py`, `src/agentgate/server/routes/comparisons.py` |

## Trace And Target Execution

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Trace model | Represent normalized Agent behavior | `src/agentgate/domain/trace.py` |
| [x] | OTel capture | Capture real demo Agent spans | `src/agentgate/integrations/observability/in_memory.py` |
| [x] | OTLP receiver | Receive external OTLP JSON traces | `src/agentgate/integrations/observability/otlp_http_receiver.py` |
| [x] | Demo Agent adapter | Execute the Loan Agent | `src/agentgate/integrations/targets/demo_loan.py` |
| [x] | Target protocol | Standardize one Case execution | `src/agentgate/run/target_protocol.py` |
| [x] | Trace redaction | Remove secrets and private data before evaluation or display | `src/agentgate/trace/redaction.py` |
| [ ] | HTTP Agent adapter | Invoke Dify, Coze, or customer Agents | `src/agentgate/integrations/targets/http_agent.py` **new** |
| [ ] | Local process adapter | Execute CLI-based Agents | `src/agentgate/integrations/targets/process_agent.py` **new** |
| [ ] | Trace replay adapter | Evaluate an existing Trace without reinvoking an Agent | `src/agentgate/integrations/targets/trace_replay.py` **new** |
| [x] | Trace cleanup | Remove obsolete Trace scaffolds after migration | `src/agentgate/trace/` |

## Run, Queue, And Scheduler

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Run Engine | Execute every Case and invoke selected Evaluators | `src/agentgate/run/engine.py` |
| [x] | Reproducible Case subset | Pin ordered Case IDs in the RunManifest and execute only that selection without changing the Dataset version | `src/agentgate/domain/run.py`, `src/agentgate/run/engine.py` |
| [x] | Worker claiming | Prevent two workers from executing the same Run | `src/agentgate/storage/sqlite.py` |
| [x] | Incremental persistence | Save each Case's Results as soon as evaluation finishes | `src/agentgate/run/engine.py` |
| [x] | Dispatcher protocol | Define whole-Run submission and cancellation through `submit(run_id)` and `cancel(run_id)` | `src/agentgate/integrations/job_dispatchers/protocol.py` |
| [x] | Dispatch workflow | Submit persisted Runs and fail dispatch errors safely | `src/agentgate/application/run_management.py` |
| [x] | Run cancellation | Atomically cancel pending/running Runs, revoke queued delivery, and cooperatively stop active execution | `src/agentgate/storage/sqlite.py`, `src/agentgate/application/run_management.py`, `src/agentgate/run/engine.py`, `src/agentgate/integrations/job_dispatchers/celery.py`, `src/agentgate/server/routes/runs.py` |
| [x] | Stale-Run recovery | Fail Runs abandoned by an expired worker | `src/agentgate/application/run_management.py` |
| [x] | Progress projection | Calculate completed Cases and Run progress from Results | `src/agentgate/application/result_reader.py` |
| [x] | Activity projection | Return queued, running, and recent terminal Runs | `src/agentgate/application/result_reader.py` |
| [x] | Celery dispatcher | Submit `run_id` through standalone Redis or Redis Cluster selected by environment configuration | `src/agentgate/integrations/job_dispatchers/celery.py`, `src/agentgate/integrations/job_dispatchers/redis_cluster_transport.py` |
| [x] | Celery worker | Load and execute the persisted Run with the same optional Judge catalog and task-local client cleanup | `src/agentgate/integrations/job_dispatchers/celery.py` |
| [x] | Scheduled Runs | Persist one-time future execution, atomically release due Runs, and expose query/cancellation through Run APIs | `src/agentgate/domain/run.py`, `src/agentgate/application/run_scheduling.py`, `src/agentgate/storage/sqlite.py`, `src/agentgate/integrations/job_dispatchers/celery.py`, `src/agentgate/server/routes/runs.py` |
| [ ] | Customer scheduler integration | Accept work from an external Java scheduler through the shared Run boundary | `src/agentgate/server/routes/runs.py` or `src/agentgate/integrations/job_dispatchers/`; planned after POC |
| [x] | Retry mechanics | Retry only classified Target infrastructure failures with bounded backoff and a fresh execution identity; never retry evaluation failures | `src/agentgate/run/retry.py`, `src/agentgate/run/engine.py`, `src/agentgate/application/run_management.py`, `src/agentgate/server/routes/runs.py` |
| [ ] | Local process management | Start, monitor, limit, and stop local Agent processes | `src/agentgate/run/process_manager.py` **new** |
| [ ] | Run Artifact collection | Register files and reports produced during execution | `src/agentgate/run/artifacts.py` **new** |
| [x] | Run cleanup | Remove legacy core, scheduler, lifecycle, model, and adapter placeholder files | `src/agentgate/run/` |

## Application And Server

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Dataset application service | Coordinate Dataset workflows | `src/agentgate/application/dataset_management.py` |
| [x] | Run application service | Coordinate Run creation, dispatch, and execution | `src/agentgate/application/run_management.py` |
| [x] | Result reader foundation | Read persisted Runs, Results, Traces, and reports | `src/agentgate/application/result_reader.py` |
| [x] | FastAPI foundation | Expose current Dataset, Run, Result, and Trace APIs | `src/agentgate/server/` |
| [x] | Target catalog | Register, list, and resolve exact immutable Target descriptors | `src/agentgate/application/target_catalog.py` |
| [ ] | External Target metadata adapters | Read Agent and Skill metadata from Dify, Coze, or customer platforms | `src/agentgate/integrations/targets/`; planned after POC |
| [x] | Evaluator management | Persist user identities and drafts, publish immutable versions, control availability, select exact specifications, and compose supported implementations | `src/agentgate/application/evaluator_management.py`, `src/agentgate/evaluator/versioning.py`, `src/agentgate/storage/sqlite.py` |
| [x] | Evaluator Catalog API | Expose built-in and user identities, drafts, publication, exact versions, enable state, and constrained deletion | `src/agentgate/server/routes/evaluators.py` |
| [x] | Judge API/worker wiring | Create API manifests and reconstruct worker execution from identical optional Judge configuration with process/task lifecycle cleanup | `src/agentgate/application/evaluator_management.py`, `src/agentgate/server/`, `src/agentgate/integrations/job_dispatchers/celery.py` |
| [ ] | Model provider management API | Configure provider endpoints, model options, and secret references without exposing credentials | Design required before implementation |
| [x] | Skill analysis workflow and API | Resolve exact Targets, run static analysis, persist reports, review findings, and expose HTTP endpoints | `src/agentgate/application/skill_analysis.py`, `src/agentgate/server/routes/skill_analysis.py` |
| [x] | Lineage queries | Find Runs by Dataset, Case, Target, Skill, or Evaluator version and construct relationship graphs | `src/agentgate/application/lineage_queries.py`, `src/agentgate/server/routes/lineage.py` |
| [x] | Asynchronous Run API | Create a Run, dispatch it, and return `202 Accepted` | `src/agentgate/server/routes/runs.py` |
| [x] | Run activity API | Expose queue, running status, progress, and history | `src/agentgate/server/routes/runs.py` |
| [x] | Historical Run rerun API | Create and dispatch a new Run from an exact terminal Run manifest without mutating history | `src/agentgate/application/run_management.py`, `src/agentgate/server/routes/runs.py` |
| [ ] | API contract review | Finalize response models and sanitized error behavior | `src/agentgate/server/` |

## CLI

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | CLI refactor | Call the same application services used by FastAPI | `src/agentgate/cli/` |
| [x] | Dataset commands | Import, export, list, publish, and inspect Datasets | `src/agentgate/cli/dataset_commands.py` |
| [x] | Run commands | Execute Runs and inspect queue or execution status | `src/agentgate/cli/run_commands.py` |
| [x] | Result commands | Retrieve reports, metrics, failed Cases, protected Traces, and Gate conclusions | `src/agentgate/cli/result_commands.py` |
| [x] | Legacy cleanup | Remove CLI dependencies on the old Control Plane and Run core | `src/agentgate/cli/`, `src/agentgate/application/` |
| [x] | CLI tests | Verify commands through application boundaries | `tests/test_cli.py`, `tests/test_cli_*_commands.py` |

The CLI now composes the same Dataset, Run, and Result application boundaries used by
the server. Removing the remaining legacy Control Plane test callers is separate cleanup.

## Web

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Dataset workspace foundation | Browse and edit Dataset content | `web/src/pages/DatasetWorkspace.vue` |
| [x] | Web routing | Provide Vue Router navigation for implemented pages | web/src/router/, web/src/layouts/AppLayout.vue |
| [ ] | Overview | Show Dataset and Run status statistics | `web/src/pages/OverviewPage.vue` **new** |
| [x] | Run workspace | Show lifecycle counters plus queued, running, and historical work | `web/src/pages/RunWorkspacePage.vue` |
| [x] | Progress polling | Refresh every two seconds while active work exists and stop at terminal state | `web/src/api/runs.ts`, `web/src/pages/RunWorkspacePage.vue` |
| [ ] | Result center | Browse completed and failed Runs | `web/src/pages/ResultCenterPage.vue` **new** |
| [ ] | Result detail | Show metrics, release gate, badcases, evidence, and Trace attribution | `web/src/pages/ResultDetailPage.vue` **new** |
| [ ] | Evaluator management | Configure Rule, Judge, and Hybrid Evaluators | `web/src/pages/EvaluatorWorkspacePage.vue` **new** |
| [ ] | Model provider settings | Configure provider connections and available Judge models | `web/src/pages/ModelProviderSettingsPage.vue` **new** |
| [ ] | Skill analysis | Display Skill conflicts and prompt mismatches | `web/src/pages/SkillAnalysisPage.vue` **new** |
| [ ] | Optimizer | Display failure clusters and suggestions | `web/src/pages/OptimizerPage.vue` **new** |
| [x] | Browser verification | Verify desktop and mobile workflows against Redis, Celery, FastAPI, and SQLite | `web/tests/` |

Visible Web labels remain Chinese. Source identifiers, API fields, TypeScript names,
and comments remain English.

## A/B Testing

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | A/B definition | Bind two versions of one Agent to the same Dataset and Evaluator configuration | `src/agentgate/application/ab_testing.py` |
| [x] | A/B execution | Create and independently dispatch two ordinary EvaluationRuns | `src/agentgate/application/ab_testing.py` |
| [x] | Two-Run comparison foundation | Compare compatible Runs by metrics, Cases, and failure movement | `src/agentgate/result/comparison.py`, `src/agentgate/server/routes/comparisons.py` |
| [ ] | Significance | Calculate confidence and statistical significance | `src/agentgate/result/statistics.py` **new** |
| [x] | Controlled A/B API | Select exact Evaluator versions, create the pair, and compare it later using the two returned Run IDs | `src/agentgate/server/routes/comparisons.py` |
| [ ] | A/B Web page | Display variants, differences, confidence, and winner | `web/src/pages/ComparisonPage.vue` **new** |

A/B testing composes ordinary Runs. It does not require a broad top-level
`experiment/` package for the POC. The POC persists two ordinary Runs, not an A/B
entity, and does not provide A/B history, experiment identity, or A/B lineage.

## Skill Analysis And Optimizer

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Skill analysis domain | Define static analysis findings and reports | `src/agentgate/domain/skill_analysis.py` |
| [x] | `[CODEX-SKILL]` Skill relationships | Detect overlap, conflict, duplication, and routing ambiguity through bounded pairwise LLM checks | `src/agentgate/skill_analysis/relationships.py` |
| [x] | `[CODEX-SKILL]` Persistence and review | Store immutable reports and one current human review per finding | `src/agentgate/storage/repository.py`, `src/agentgate/storage/sqlite.py` |
| [x] | `[CODEX-SKILL]` Application and API | Run analysis for exact Target descriptors and expose report and review workflows | `src/agentgate/application/skill_analysis.py`, `src/agentgate/server/routes/skill_analysis.py` |
| [ ] | Automatic invocation | Optionally run static checks during Agent creation or evaluation setup | Deferred until external Target integration is designed |
| [ ] | Prompt alignment and deterministic description checks | Compare Agent Prompt, Skill descriptions, Tools, and capability boundaries | Deferred after the simple POC |
| [x] | Optimization domain contracts | Define immutable evidence, clusters, matrix, hypotheses, suggestions, and reports | `src/agentgate/domain/optimization.py` |
| [x] | Failure clustering | Deterministically group failed Results by stable evaluation dimensions | `src/agentgate/optimizer/clustering.py` |
| [x] | Observed routing confusion matrix | Measure expected versus actual Skill routing with explicit exclusions | `src/agentgate/optimizer/confusion_matrix.py` |
| [x] | Root-cause hypotheses | Generate evidence-constrained hypotheses through bounded, redacted LLM requests and strict response validation | `src/agentgate/optimizer/root_cause.py`, `src/agentgate/optimizer/root_cause_prompt.py`, `src/agentgate/optimizer/root_cause_contract.py` |
| [x] | Reviewable suggestions | Derive targeted recommendations from validated LLM hypotheses while requiring human review | `src/agentgate/optimizer/suggestions.py` |
| [x] | Optimizer pipeline | Compose deterministic clustering and routing analysis with an injected model boundary | `src/agentgate/optimizer/pipeline.py` |
| [x] | Optimizer application and API | Load persisted Results and Traces, reuse configured model access, and expose safe provider-failure responses | `src/agentgate/application/optimization_analysis.py`, `src/agentgate/server/routes/optimizer.py` |
| [x] | Optimizer cleanup | Remove the rejected generic service wrapper | `src/agentgate/optimizer/service.py` |

Optimizer backend implementation and LLM root-cause integration are complete on
`feature/llm-root-cause-analysis` and documented in
`docs/optimizer/implementation-plan.md`. The full regression passes and the feature is
committed and pushed; review and merge remain.

## Verification And Delivery

| Status | Capability | Function | Code location |
|---|---|---|---|
| [x] | Current backend regression | Verify the integrated backend including LLM root-cause analysis | `tests/` - 909 passing, 1 existing warning |
| [x] | Redis/Celery integration | Verify standalone configuration plus real three-master Redis Cluster broker delivery, worker consumption, and same-slot broker keys | `tests/test_celery_dispatcher.py`, `tests/test_redis_cluster_transport.py`, `tests/storage/test_redis_cluster_queue.py`, `web/tests/`, operational smoke |
| [x] | Browser verification | Verify all currently implemented desktop and mobile workflows | `web/tests/` - 8 passing |
| [x] | Documentation | Explain setup, APIs, Redis, Celery, and demo operation | `README.md`, `web/README.md`, `docs/` |
| [ ] | Repository cleanup | Delete obsolete placeholders and compatibility code | Entire repository |
| [ ] | Demo packaging cleanup | Move standalone demo behavior out of the reusable AgentGate package if still appropriate | `src/agentgate/demo/`, `examples/` |
| [x] | Async slice regression | Run backend, frontend, and browser suites for the asynchronous vertical slice | Entire repository |
| [ ] | Delivery | Commit, push, and tag the completed refactor POC | Git repository |

## Deferred Production Capabilities

- PostgreSQL migration and high-availability Redis.
- Authentication, authorization, tenant isolation, quotas, and audit integration.
- Dependency-based evaluator short-circuiting with explicit blocked/skipped Results and
  `blocked_by_evaluator_id` provenance.
- Priority queues, tenant fairness, multiple worker pools, and resource-aware routing.
- Recurring schedules, scheduling priorities, and calendar rules.
- Immediate interruption of arbitrary blocking Target calls and persisted per-attempt
  cancellation history.
- Customer-specific Java scheduler and Agent-platform adapters.
- Production observability platform integrations and external Result callbacks.
- Automated resume or retry of partially completed Runs.
- Persisted per-attempt retry history and retry events in Traces; the POC stores only the
  successful execution Trace.
- Persisted A/B identity, pair history, and A/B-specific lineage after the POC.
- Semantic or embedding-based failure clustering.
- Persisted Optimization Reports and cross-Run history.
- Suggestion review and application lifecycle.
- Automatic regression Run creation from accepted suggestions.

### 2026-09-27 · V2 逐评估器标注

完成本次运行快照驱动的评估器列表、规则证据/结果/关键代码与左右联动折叠、LLM 实际请求提示词留存和历史重建标识、逐评估器评分与提示词保存。操作步骤见 docs/annotation/end-to-end-guide.md。后端全量回归 1337 passed / 35 skipped，前端 60 项测试及构建通过；规则浏览器闭环通过，LLM 使用录制模型验证，尚无本地历史 LLM 会话供真实页面验收。


### 2026-09-29 · 样本执行轨迹与完整 JSON 联动

样本详情的测评模块下方接入 TraceExplorer：左侧按父子关系展示执行节点、状态和时间条，右侧展示接口返回的完整只读 Trace JSON。点击节点展开相关折叠范围、滚动并高亮对应 Span；切换样本重置选中状态。trace-presentation 仅整理显示数据，保留原始 JSON 顺序和未知字段，处理缺失父节点、循环关系、缺失或无效时间。

验证：17 项相关单元测试、类型检查、局部 ESLint/Prettier 和生产构建通过；隔离浏览器验证覆盖折叠展开、只读、重置及窄屏，当前本地应用已有样本通过全部节点展示、JSON 完整性、点击定位和高亮检查。仅可展示接口已返回的节点，不补造缺失链路。

### 2026-10-06 · 历史标注功能恢复（隔离分支）

从 10 月 5 日遗留的 Git stash 快照恢复 V2 模板创建、会话列表与标注、评估器证据、测评集反馈及 Trace 图。恢复分支 `feature/restore-annotation-v2` 以 `refactor-1` 为起点并对齐 `integration/baibo` 的已提交进度；现有工作区的未提交 Agent 拓扑改动未纳入。数据集创建页面沿用当前已更新的行内/行外目录与图谱实现，不覆盖为旧快照。V2 模板继续保存在浏览器本地存储，旧 V1 数据使用原有存储键。

验证：前端类型检查、lint、生产构建和 75 项单元测试通过；后端全量回归 1348 passed / 35 skipped。隔离页面 `http://127.0.0.1:5198/#/annotations` 已完成行外登录、选择本地智能体与版本、创建 V2 模板、刷新后重新登录验证卡片恢复，并进入 V2 会话工作台。隔离数据库没有已完成运行，所以逐轮标注的页面闭环尚未实测。分支尚未合入 `integration/baibo`。


### 2026-10-06 · 历史改动二次核对与弹窗宽度恢复

核对来源：`recovery/annotation-v2-snapshot`（`d18301f`）、其未跟踪文件快照、当前 `integration/baibo` 提交、Git reflog 中的遗留提交，以及「记录并优化需求描述」历史会话。历史快照的 60 个已跟踪改动文件和 21 个新增文件在恢复工作树中均存在；恢复时已适配的目录、类型、存储与行内模型修复单独检查，未用旧文件覆盖新实现。`origin/goal/p1-demo` 与 `origin/integration/p1-new` 没有当前前端样式文件，本轮使用当前前端及用户历史要求恢复，不变更架构。

| 核对项 | 发现与处理 |
| --- | --- |
| 创建测评集弹窗 | 后续表单替换丢失了原 800px 样式；恢复为 `min(800px,96vw)`，内容区域保持 68vh 上限。 |
| 标注模版、详情等弹窗 | 全局 `width: min(400px,92vw) !important` 压过各组件声明宽度；改为遵循 Element Plus 的宽度变量，未声明宽度仍默认 400px，限制不超过 96vw，全屏弹窗排除普通尺寸限制。移除任务、样本向导、LLM 设计中已无必要的强制宽度补丁。 |
| 创建测评集字段 | 恢复“01 测评集名称”、名称下方单行选填描述、“02 关联智能体”，场景标签明确选填；清除残留的“数据集”页面文案和一个多余的 > 字符。 |
| 标注模版默认描述 | 恢复历史快照中“通用消息与工具评分维度，复制后可调整评分范围与标签。”，不改写用户已保存模板。 |
| Agent 目录与图谱 | 保留 10 月 6 日新实现及行内／行外路由；主工作区尚未提交的任务拓扑改动仍在原处，没有混入恢复分支。 |
| V2、Trace、样本期望、评估器证据 | 对照快照，相关新增实现和测试均存在，未发现进一步的整文件遗漏；这不替代真实模型及所有历史需求的端到端验收。 |

浏览器实测：1440px 视口下创建测评集 800px、创建 V2 模版 1080px，均按声明宽度生效；默认窄窗口下 V2 为视口的 96%，弹窗无横向溢出。名称顺序、描述单行和选填文案已检查。前端 75 项单元测试、类型检查、lint 和生产构建通过；本轮没有修改后端。代码仍位于 `feature/restore-annotation-v2` 的隔离工作树，尚未合入主工作区。


### 2026-10-06 · 验收页面加载历史测评数据

5198 页面此前使用 `/private/tmp/agentgate-annotation-restore.db`，仅含自动初始化的 1 个贷款演示测评集，无运行记录。只读核查发现原工作区 `runtime/agentgate.db` 保留 11 个测评集、21 个版本、26 个当前格式任务记录、38 次运行和 98 条 Trace。通过 SQLite backup 创建独立副本 `/Users/baibo/915-HN-AgentGate/runtime/recovery-preview/agentgate-history-20261006.db`，完整性检查为 ok，随后将 8098 验收后端的 `AGENTGATE_DB` 切到该副本。原库及此前临时库均保留；页面此后的编辑仅影响副本。

新版接口实际返回：测评集 11 条、任务记录 26 条、运行 38 条，均 HTTP 200。浏览器已确认测评集列表共 11 个；任务默认近 7 天只有 1 项，切到近 30 天后为 32 项（26 个任务以及 6 个未绑定任务的历史运行）。该切换为当前页面筛选，没有改动用户之前要求的近 7 天默认值。历史记录可继续用于 V2 与 Trace 验收，此前“隔离库没有已完成运行”的限制已解除。

另发现独立模拟环境 `runtime/agent-platform/local/agentgate.db` 中有 2 个测评集、10 个任务、12 次运行，以及旧交付快照 `delivery/2026-09-16/agentgate.db` 中有 18 个测评集、53 次运行。它们属于不同环境/旧结构，本次未混合或覆盖到主历史副本。默认启动只初始化贷款测评集和目标定义，不自动生成历史任务；平台模拟和三种贷款模式另有种子脚本。


### 2026-10-06 — 历史智能体接入关联目录

行外 localAgentDirectory 组合 8119 的三类平台模拟智能体与 API 本地目录：Loan Agent 两版本、8107 的贷款基础编排/工作流/云虾三模式，共 7 个逻辑智能体。贷款服务不可达时显示离线条目；行内仍使用 agentDirectory 和行内 token。版本选项传递真实描述符与明确执行类型，贷款云虾无需平台分支。新建任务分别提交到 platform、bank 或 demo 执行入口；测评集拓扑使用本地真实描述符；V1/V2 标注保存 source/adapter 并按历史身份匹配会话。已通过 UI 创建“接入验收 · 贷款云虾”V2 模板，匹配 12 条既有会话。

运行环境沿用独立历史副本，新增隔离队列 6398、Worker、贷款服务 8107、读取本次 SDK 输出的 Trace Server 8218。原有 8210 服务保持不动。模型复用旧工程外部环境文件，密钥未写入代码；上游 glm-5.3 请求返回 HTTP 403，三种贷款新运行诚实记录 failed，真实模型验收尚未通过，需要可用模型配置。平台模拟基础两版本、工作流及云虾两分支共 5 次运行 completed，内置 Demo 两版本共 2 次 completed，7 次成功运行均可读取新 Trace。验收结果保存在主目录 runtime/recovery-preview/local-agent-verification.json。

验证：前端 lint、typecheck、76 项单测及生产构建通过；后端 1349 passed、35 skipped。新增测试覆盖本地目录真实来源/版本、离线条目、任务名称、标注来源与登录模式隔离。当前变更保留在恢复工作树，未合并、未覆盖原工作区未提交内容。

补充页面验收：直接在“新建测评任务”选择 Loan Agent → loan-agent-v2-fixed → 高风险贷款策略评估 v1，提交“接入验收 · 页面选择 Loan Agent”。运行 1a738eda-cc31-40e4-87b8-88fb47f21d59 完成，manifest adapter_type=demo_loan，页面显示 1/1、100 分。截图保存在主目录 runtime/recovery-preview/all-local-agents.png 和 loan-history-associated.png。


### 2026-10-06 — 三种贷款智能体切换 DeepSeek v4 Pro

已使用环境中的有效 DEEPSEEK_API_KEY（不落库/不写源码）连接 https://api.deepseek.com，模型 deepseek-v4-pro，BANK_MODEL_THINKING=disabled。原 8107 服务已重启，基础编排、工作流、云虾的描述符和实际 Trace 均报告 deepseek-v4-pro。保留旧运行及旧描述符，不修改历史证据；原 glm-5.3 403 阻塞已通过本次模型切换解除。LLM 评估器连接不在本次范围内。

LiveModel 支持显式 thinking 参数并校验取值，工具消息保留 reasoning_content 以满足 DeepSeek 协议；未配置 thinking 时不发送该参数。tested-agents 全部 23 项测试通过（含三种配置、工具循环协议、JSON 输出及密钥不进入 Trace）。参考官方 Chat Completions / Thinking Mode 文档： https://api-docs.deepseek.com/api/create-chat-completion/ ，https://api-docs.deepseek.com/guides/thinking_mode/ 。

真实验收：三模式各跑 high 和 multi 两个合成样本，共 6 样本、9 轮，3 任务全部 completed、综合分均 1.0、18 项评估器结果均 pass。逐个检查新 Trace 的 trace_sdk.model=deepseek-v4-pro；高风险结果 pending_review，多轮补齐结果 approved。

- base：aec9ffe7-9e78-480b-8aa5-a49c89bb688f
- workflow：1f4c78f8-783d-4a47-916e-ce90978bcc33
- cloudshrimp：8e375007-4d9e-49de-bd1f-157bea38dd86

本机启动脚本：主目录 runtime/recovery-preview/run-bank-deepseek.py（从环境读取密钥）；验收记录 deepseek-v4-pro-verification.json，页面截图 deepseek-v4-pro-tasks.png。测评任务页搜索“DeepSeek v4 Pro 验收”即可查看三条结果和样本 Trace。上述 100 分仅代表这组规则验收样本。


### 2026-10-06 — 贷款云虾 LLM 评估器配置

复用现有 answer_quality 实现、Evaluator Catalog 版本发布、full_trajectory 证据选择及 Judge 严格 JSON 契约，未新增执行框架或修改被测智能体。创建并启用“贷款云虾 · 回答可信度与审批解释”（113ee56f-3136-4d7d-9b43-ab47dfaeea06），当前发布 v3。评分提示词定义事实一致性40%、审批原因解释30%、请求与多轮处理20%、测试边界10%，通过阈值0.85、最低置信度0.75；原因混淆最高0.79，关键事实矛盾/编造审批/真实放款最高0.49，关键证据不足转review。权重由LLM依据评分提示计算，后端目前仅持久化一个整体score，不提供独立的维度得分字段。

v1试运行发现模型把信用评分的 [redacted] 当成事实矛盾；v2明确脱敏值不属于可核验数值，不据此扣分或反推出值。后续发现“因为高风险（存在阻断标记）”被过宽解释，v3加入并列归因、括号归因、简洁正确归因等评分锚点。7类校准样例全部符合预期：原历史混淆回答与括号变体均0.79/fail，两种正确回答均1.0/pass，虚构审批及提示注入均fail，独立证据缺失review。这些是开发者构造的校准样例，尚非标注一致率或泛化准确率。

本地预览API/Worker的默认Judge连接已由旧GLM切换为 https://api.deepseek.com / deepseek-v4-pro，密钥只读环境 DEEPSEEK_API_KEY，通过 env:AGENTGATE_JUDGE_API_KEY 引用，不写入配置JSON。此默认连接也由当前预览的Skill分析和根因分析复用；不修改行内网关或登录Token路由。被测目标使用行外local_bank云虾，synthetic token=local。所有验收数据来自内置合成测试客户；发送前沿用工程脱敏，不解除脱敏。

配置与验收证据位于主工作区 runtime/recovery-preview/：cloudshrimp-loan-judge.json（可重建的创建请求）、cloudshrimp-loan-judge-published.json（v3快照）、cloudshrimp-judge-calibration.json（7类校准结果）。保留v1/v2任务作为校准试运行，不改历史结果。最终v3验收任务 a730b7ed-ec57-4660-b34a-e4176e025f85 使用贷款云虾测评集v1全部8个场景，并同时运行 final-state、required-tool、forbidden-tool 和本LLM评估器。

最终验收完成：a730b7ed-ec57-4660-b34a-e4176e025f85 全部8样本完成，耗时174.3秒，无执行或评估异常。8项LLM结果均留存deepseek-v4-pro真实Judge请求/响应，7通过、1失败、0复核，LLM均分89.875；阻断样本因拒绝原因混淆得到79分/fail。规则检查20通过、4不适用，无失败。页面综合分96.6包含规则分，不等于LLM均分；独立维度分数未返回，页面诚实显示“—”，样本详情“评估结论与依据”显示LLM整体分与问题证据。完整输出 cloudshrimp-judge-v3-report.json、摘要 cloudshrimp-judge-verification.json、截图 cloudshrimp-judge-v3-result.png 均保存在主目录 runtime/recovery-preview/。已通过浏览器确认自建评估器启用、最终任务8/8完成及失败样本79分原因。此次仅配置和文档变更，未改业务源码，未进行无关全量回归；现有行内目录/Token路由保持不变。


### 2026-10-06 — 修复LLM结果分数列为空

结果表曾将LLM rubric的每条标准展开成独立分数列，而Judge契约只返回评估器整体score，导致已有79分等真实评分未在主表展示。现按运行快照中的主评估器各生成一列，直接读取对应EvaluationResult.score；任务结果、样本详情评分条及CSV共用逻辑。评分区改称“评估器得分”，不虚构独立维度分数，不改变历史分数和总分算法。任务a730b7ed-ec57-4660-b34a-e4176e025f85页面已验证LLM列依次显示79/85/100/85/85/100/85/100，样本详情显示79分。7项结果逻辑测试、前端typecheck与lint通过。截图：主工作区runtime/recovery-preview/llm-score-columns-fixed.png。


### 2026-10-06 — Skill 静态分析页面接通与真实验收

复用现有 skill_analysis 职责关系分析、不可变报告、人工复核与任务关联接口，接通当前 TaskResults 的“Skill 静态分析”页签。按本次运行的精确 TargetDescriptor 分析，读取历史报告并持久关联任务；不重新执行测评用例。新建任务支持对行外本地已注册描述符预先分析并保存报告关联，切换智能体/版本/模式会清理过期结果；行内不回退到本地 Demo，运行后可按任务描述符分析。少于两个 Skill 时明确不适用。未新增目录或后端抽象。

页面展示逐对关系、模型置信度、完成数量、问题依据/建议、人工复核和原始报告，明确只覆盖职责描述重叠/冲突/重复/歧义，不表示代码安全、Prompt-Tool 一致性或实际路由准确率。云虾任务 a730b7ed-ec57-4660-b34a-e4176e025f85 已通过 DeepSeek v4 Pro 真实分析：3 Skills、3/3 比较、completed、0 findings、0 errors，三对关系均 none（模型置信度0.95）。报告 54d4b198-5a50-4ee8-9f13-ca3d94962076 已持久关联，切换页签后可重新读取。

验证：前端 lint、78 项单测、类型检查及生产构建通过；Skill 分析模型/应用/存储/API及任务关联相关后端57项测试通过。证据：主工作区 runtime/recovery-preview/cloudshrimp-skill-static-verification.json 与 cloudshrimp-skill-static-analysis.png。验收入口：任务详情 → Skill 静态分析。


### 2026-10-07 — 贷款工作流图谱区分完成

runtime.py 共用执行与导出的 StateGraph 定义，保持7个业务节点、开始/结束、10条连线及 workflow.* Trace 名称。/agents 返回 workflow topology；local_bank.py 校验节点ID、类型和连线端点后固定到描述符，异常图拒绝，不回退为工具图。

TargetStructure.vue 对工作流按有向层级展示，标注LLM/规则/工具/起止、箭头和三条条件分支。基础编排仍为 Agent → Tool（7节点/6边），云虾为 Agent → Skill → Tool（10节点/10边），工作流9节点/10边。点击节点显示职责及关联条件；循环图保留关系并提示不作线性排序。此轮只区分结构图，没有新增编辑画布或Trace到JSON联动。

本地预览8107、8098及Worker已更新。三类图谱已逐项页面验证，历史任务1f4c78f8-783d-4a47-916e-ce90978bcc33保留原图快照，行内/行外目录与token路由不变。入口：测评任务 → 新建测评任务 → 贷款智能体 · 工作流 → v1 → 智能体图谱。

验证：贷款服务29项、适配器相关133项、前端81项测试通过；前端lint、类型检查与构建通过。主工作区 runtime/recovery-preview/workflow-topology-verification.json 保存实际目录及历史快照证据；workflow-agent-topology.png 为页面截图。


### 2026-10-07 — 三类贷款智能体核心专项测评完成

按用户授权直接完成实施和验收。复用当前多轮 Case/Expectation、状态/工具/Skill 路由规则、answer_quality full_trajectory、已发布评估器和 composite(all)；新增 ExecutionPathExpectation 与 execution_path 规则实现，未扩展默认内置评估器列表，使用已发布的用户规则配置。规则逐轮检查工具顺序/次数/额外调用、完整工作流路径、真实 Skill 执行及工具祖先归属，要求成功的轮根节点和成功的执行状态；根据开始时间排序，避免完成事件顺序误判。local_bank 将真实 workflow.* / skill.* SDK spans 规范化，未构造执行证据。前端保存、JSON/Excel 导入导出保留新断言，样本编辑页展示只读明细。

基线评估：本地未能解析 goal/p1-demo 与 integration/p1-new，不能声称已比较其实现；本次采用当前恢复分支既有领域、适配器、评估器与数据集契约，新增路径能力独立于目录查询，未改变行内/行外目录和 Token 分流。真实验收在行外本地预览进行，行内依赖回归测试，未连接实际行内网关。

创建3套专项集，每套8样本、10轮：基础编排覆盖审批三分支、缺资料、多轮补齐、查询和绕过政策；工作流覆盖全部7业务节点与10条图连线（含逻辑起止边），包括完整申请、查询、缺资料/咨询短路；云虾覆盖 loan_application/application_status/general_help 三个 Skill、跨轮路由切换、禁止未注册 Skill 和工具归属。三类申请后查询样本均从实际状态核实申请编号连续。只统计这组核心场景，不代表所有输入或异常路径完整覆盖。

初次配置校验发现 tool_argument 路径应为 arguments.amount / arguments.purpose，已发布数据集v2，旧任务改名“配置校验 v1”并保留结果，未篡改历史。最终任务全部锁定v2，每个智能体各运行规则、LLM、规则＋LLM三种配置，共9任务、72样本、90轮；被测模型与48条真实 Judge 记录均为deepseek-v4-pro。全部任务 completed，无执行/评估异常、无待复核；67样本通过、5样本失败。

| 智能体 | 规则 | LLM | 规则＋LLM |
| --- | --- | --- | --- |
| 基础编排 | 8/8 | 7/8 | 8/8 |
| 工作流 | 8/8 | 7/8 | 7/8 |
| 云虾 | 8/8 | 7/8 | 7/8 |

以上为通过数。五条失败均为阻断客户回答的原因混淆，LLM给79分：正确执行拒绝，但未明确 blocked 是决定性原因。独立任务重新执行智能体，输出不同，基础编排联合任务通过不代表与LLM任务共享同一回答。联合任务使用 all 门禁，子LLM失败即整体失败；均分仍沿用现有聚合算法，云虾失败样本综合97分也显示未通过，不将高均分当作通过。

入口：测评集搜索“核心专项”；测评任务搜索“核心专项验收”。主目录 runtime/recovery-preview/loan-specialized-v1-index.json 保存3集、7个发布评估器、9任务ID（文件名历史保留，内容数据集v2）；loan-specialized-summary.json 为完整摘要；loan-specialized-coverage.json 为逐轮路径、Skill、模型与编号连续性审计；loan-core-*-results.json 为结果。create-loan-specialized.py 已同步正确参数路径。前端截图 loan-specialized-final.png。

验证：后端1380通过、35跳过（2条现存告警）；前端82项测试通过，lint、类型检查和生产构建通过（既有包体积提示）。页面核实工作流路径对照、云虾第二轮 Skill 切换、联合任务真实分数和失败门禁。临时增加的两个 Worker 在任务完成后停止，常驻预览API/Worker/贷款服务保留，未提交或合并已有恢复分支改动。


### 2026-10-07 — 核心专项测评集随源码分发

复用 demo/bootstrap.py 的启动初始化职责，新增包内 loan-core-datasets.json 保存三套最新v2合成样本（24样本30轮），无本机路径、模型连接、任务或凭据。SQLite API启动自动原子写入缺失的数据集及发布版本；已存在ID完全保留，包括归档、用户修改和后续版本。MySQL启动不自动写入测试集，未改变行内/行外智能体目录和token路由。空数据库API测试已确认原示例加三套专项集可见，重启持久化测试验证无重复、无覆盖。README补充下载和查看方法；本次未提交或推送，分发需使用包含这些变更的源码。

最终验证：后端全量1381通过、35跳过（2条既有告警）；新安装API可见性及重复启动保护6项针对性测试通过。


### 2026-10-07 — 金额审批策略用例v3

读取本地贷款测试数据库：已有5万拒绝、8万三种结果、20万通过、30万转人工记录；profiles只有test-low(720/low/未阻断)、test-high(580/high/未阻断)、test-blocked(400/high/阻断)。审批函数先判blocked拒绝，再判高风险或评分<650或amount>200000转人工，否则通过。三套核心集发布v3：保留原场景，将阻断样本改5万，新增199999/200000/200001/300000元低风险案例，每套12样本14轮。199999和200001为依据规则新增的边界输入，不声称原数据库已有记录。同步参数金额、状态和工具路径断言、源码种子及README；历史v2和既有任务结果保留。本次验证用例配置与真实决策函数，不重新执行付费模型测评。


### 2026-10-08 — 提交至 integration/baibo

用户授权将当前恢复分支成果提交并推送到远程baibo分支，实际目标为origin/integration/baibo。包含标注v2恢复、完整Trace展示、贷款图谱与Skill分析、核心专项规则与v3种子数据，以及样本详情结果栏加宽与单轮轮次目录统一。主工作区另存的EvaluationTaskForm.vue与agent-topology.ts未纳入本次提交；不包含运行数据库、依赖软链接或凭据。提交前后端1381通过/35跳过，前端82通过，贷款运行时29通过；前端lint及生产构建通过。

### 2026-10-23 — Evaluator and dataset interface cleanup

Implemented the requested interface changes on `feature/ui-evaluator-dataset-polish`, based on updated `refactor-1`. Built-in rule cards hide the English subtitle, rule details omit execution implementation metadata, and LLM cards omit the fixed category tags and consistently label their listed items as child evaluators. The LLM template picker omits the scenario sidebar section. Evaluators now appear last in the main navigation. A/B result pages show comparisons without tuning or static-analysis tabs; sample details omit the footer below the trace explorer.

Dataset cards anchor their divider and update time to the bottom of each grid row. Status filters are All, Published, and Draft: published means a published version exists; draft means an editable draft exists. A dataset with both belongs to both filters. The expected-Skill input remains unchanged, as requested after clarification.

The import wizard links to a bundled Excel template with Cases, field instructions, and expectation examples. It reuses the previously verified synthetic loan template: four samples, five turns, and 22 expectations. Only Cases is imported; the instructions cover every import column, multi-turn grouping, JSON syntax, and selecting evaluators separately. The preview now counts preserved execution-path and Skill/policy checks as well as editable checks. The download uses Vite asset URLs so the `/agentgate/` deployment base is respected.

Validation: all 28 focused import/export and evaluator-group tests passed (including a new test that reads the shipped Excel file through the actual importer); frontend typecheck, changed-file lint, and production build passed. The existing bundle-size warning remains. Local UI checks covered rule details, LLM cards/template selection, both status filters, template upload preview, A/B comparison, and trace detail. Downloaded bytes match the bundled template; production output includes the Excel asset. No backend contracts or in-bank/external Agent routing changed. No commit, push, or merge was performed.

### 2026-10-23 — Align sample import, templates, and export formats

Continued the same interface feature branch. Import templates now appear only on the file-import configuration step, after Next. The wizard offers Excel, JSON, and CSV downloads, expandable JSON syntax/examples and Excel/CSV field instructions. JSON and CSV reuse the Excel template's four synthetic samples, five turns, and 22 expectations. Template links download blobs so JSON and CSV are saved reliably in the app browser. ZIP is no longer accepted or advertised; XLSX decompression limits remain intact.

The export dialog uses the same supported-format list and adds UTF-8 CSV with a BOM, quoted fields, and the complete 12-column sample schema. Both full and selected exports retain the existing version/scope controls. CSV preserves multi-turn grouping, initial state, structured input, all expectation kinds, and multiline text. No tested-Agent directory or token routing was changed.

Validation: 32 focused import/export and evaluator-group tests passed, including three-format round trips, all seven expectation kinds, CSV quoting/Unicode, ZIP rejection, and equality of the three shipped templates. Typechecking, changed-source lint, and production build passed; the existing bundle-size warning remains. Local browser checks confirmed no template links on step one, all three downloads on step two, JSON guidance, CSV export selection/download, and reimport preview of that actual exported file (four samples, five turns, 22 checks). Preview was cancelled without adding duplicate records. No commit, push, or merge was performed.

## 2026-11-02 — Chinese Judge output instructions

Make the existing answer-quality Judge output instructions and correction prompts
explicit in Chinese. The five-field verdict and per-dimension contracts remain
separate. Explain valid JSON syntax, exact fields, numeric ranges, the configured
threshold, and the existing character/item limits using the parser constants.
Require Simplified Chinese for reasons, criterion labels and violation details in the prompt, while
preserving tool names, code, identifiers, necessary source quotations and JSON field
names/fixed values. Do not introduce backend language detection or rejection rules.
Clarify lowercase English verdict values, non-null required fields, literal JSON
booleans for per-dimension review, and unchanged configured dimension IDs. Criterion
labels are display text; dimension names and weights in results remain populated
from configuration by the backend, rather than added to the model output contract.
Derive the recommended output budgets from half of each existing parser limit:
300 characters for reasons and violation details (hard limit 600), 100 characters
for a criterion (hard limit 200), 10 violation entries (hard limit 20) and 32,000
characters for the complete response (hard limit 64,000). These are prompt targets only:
preserve independent violations, and allow a larger response when needed to cover
every configured dimension and required field.

Reuse each contract's instructions for the first request and the single correction;
include the actual validation error as quoted diagnostic data. Preserve configured
business criteria, evidence, scoring, parsers, timeout budgets and attempt records.
Both SDK and HTTP providers receive the same strengthened system instructions.
No frontend, persistence, transport-parameter or scheduler changes are introduced.

Nine related backend modules passed **325 tests**, including malformed JSON, extra
fields, overlong reasons/details, invalid scores, correction success and repeated
failure. This total includes **183 tests** in the four focused Judge modules, covering all required
fields being missing/null, translated or incorrectly cased verdicts, numeric strings,
invalid violation objects, mismatched dimension IDs and non-boolean review values.
They also verify acceptance above the prompt targets while still within parser limits.
Independent review verified both JSON examples against their parsers.
Whitespace checks passed; Ruff introduces no findings beyond the existing baseline.
These are local contract tests, not live in-bank model acceptance; prompt changes
cannot guarantee model compliance.
