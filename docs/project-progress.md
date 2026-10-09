# AgentGate Project Progress

Last updated: 2026-09-23

## In-bank Trace readiness polling — 2026-09-23

- Added bounded readiness polling in the two existing in-bank adapters, covering base/workflow/Yunxia. Immediate first query, two-second poll delay, stop on readiness without replaying chat; reuse the existing request/Case timeout budget.
- Per user constraint, `trace_server.py` is unchanged. Transient retrieval failures and recognizable pending evidence retry; permanent errors still fail. Timeout continues to preserve Pod cleanup. No output-only fallback is implemented yet; that behavior and evidence-aware evaluator handling are under discussion.
- Verification: 317 focused tests passed, 26 skipped, including real local HTTP 404-then-ready checks for all three types. Changed adapter/Trace-test Ruff checks and diff whitespace checks passed. No customer calls, commit or push performed. Readiness does not establish completeness of late LLM uploads without a server watermark.

## In-bank customer-function adaptation — 2026-09-23

- On `feature/inbank-trace-server`, adapted the supplied customer lifecycle and chat functions inside the existing ChatABC and Yunxia modules. Preserved RunEngine, evaluator/report contracts, real Trace Server retrieval, eight-character task IDs, unified Pod deletion and Yunxia branchId/message fixes. No separate lifecycle module or customer Excel/DB main program was introduced.
- Creation retries three times and ChatABC session initialization retries ten times, stopping at the first success. Customer SSE extraction replaces virtual-bank parsing only in the real in-bank adapters. Responses close on success and failure; health failure still triggers registered Pod cleanup.
- Verification: 325 focused tests passed, 26 skipped; 51 direct comparisons to isolated original customer functions passed. Full backend: 1596 passed, 52 skipped, 8 failed. All eight BJS launcher failures also reproduce before this change because the workspace lacks `.venv/bin/python`. Changed adapter/contract-test Ruff checks, dependency lock validation and diff whitespace checks passed.
- `requests` and its dependencies must be included in offline deployments. Local tests do not prove customer-platform acceptance or availability of the required real Trace references. No commit, push or merge performed. See [customer adaptation boundaries](inbank-customer-adaptation.md).

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
