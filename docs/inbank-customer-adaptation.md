# In-bank customer-script adaptation

## Scope and source

The two existing adapters retain the AgentGate execution contract and incorporate
the customer's HTTP/lifecycle/chat function bodies. They do not run the customer's
standalone evaluation program or introduce a separate lifecycle module.

Sources: the supplied `workflow&bianpai.py` and `yunxia.py` original backups, plus
the customer's confirmed unified deletion, Yunxia `branchId`, and final `message`
corrections. The desktop originals and corrected copies are unchanged.

| Customer functions | Engineering location / adaptation |
| --- | --- |
| `create_agent_pod`, `wait_for_pod_ready`, `delete_agent_pod` | Kept by name in each existing in-bank adapter module. Three creation attempts, ten-second retry delay, stop immediately on success. Health uses each Agent's own path and response shape. |
| `make_request`, `AgentChatClient._build_app_payload`, `init_session` | Kept in `chatabc.py`. Base uses prompt/tool variables; workflow combines them into config variables. Ten initialization attempts, five-second delay, stop on success. |
| `AgentChatClient.chat_stream`, `_parse_sse`, `_extract_chat_answer` | Kept in `chatabc.py`. Actual `requests` streaming, customer message extraction and auxiliary-event tolerance. |
| `safe_json_loads`, `try_parse_json`, `parse_sse_stream`, `call_api_via_pod` | Kept in `yunxia.py`. Message endpoint is `/api/v1/message`; no init-session call is added. |
| `build_request_payload`, `process_single_row` | Adapted in `yunxia.py` to consume mappings instead of pandas rows and to return chat data instead of the customer's Excel result model. Final text uses `message`, with the already-supported `output` fallback. |

## AgentGate mapping

- Factory, adapter type/version, `start/get_status/wait/cancel`, trace resolvers and
  `CaseExecutionResult` remain unchanged. `close()` remains registered with the
  Worker's `ExitStack`, performs deletion once, and does not mask the original failure.
- One adapter owns one Run's Pod. Creation/health occur before the first Case;
  all Cases reuse that Pod. Each ChatABC Case initializes one Session and runs its
  Turns sequentially in that Session.
- `target.ref.external_target_id` supplies agentId; the existing target version
  mapping supplies agentVersion; `run_id[-8:]` supplies the customer taskId.
  Yunxia receives `invocation_config.branch_id` as branchId. The adapter still
  requires a real branch before creation; the copied helper retains the customer's
  optional-branch behavior when called independently.
- Current `initial_state` and Turn input contracts remain unchanged. Workflow
  config_variables are passed through the customer's initializer without requiring
  Excel-shaped frontend input. Base retains prompt_variables/tool_variables.
- Yunxia Case ID substitutes for customer questionId/sessionId. Its default custID
  is the approved test prefix plus Case ID plus a run-level random suffix. Same-Case
  Turns share it; different Cases have different defaults. An explicitly configured
  test customer ID remains supported. app_history maps to appHistory.
- Environment names remain unchanged. The existing settings are explicitly mapped
  to customer CCE_CREATE_BASE_URL, HEALTH_CHECK_BASE_URL, POD_API_BASE_URL,
  DELETE_POD_BASE_URL and AGENT_NAMESPACE keys inside the adapter.

## Deliberate differences, not claims of byte-for-byte reuse

- HTTP clients and clocks/sleep are injectable for deterministic testing. Responses
  are closed, UTF-8 SSE decoding is explicit, path/query parameters are escaped,
  and the existing 10 MiB SSE bound is retained. Health uses a monotonic wait budget.
- Default lifecycle request timeouts follow the customer: create/delete 60 seconds,
  health 10 seconds, init_session 30 seconds. Existing configurable request timeout
  still applies to chat and Trace Server retrieval; it is not a total Run deadline.
  Health's overall wait is capped by the Case timeout and configured health wait.
- The last failed attempt does not sleep. Creation failure and initialization
  failure remain distinguishable from chat failure; a chat is not automatically
  replayed by the stage-specific retries.
- Customer result dictionaries receive a classified error code at the integration
  boundary. Raw exception/response contents are not added to ordinary logs. Existing
  opt-in SSE failure diagnostics and Pod lifecycle status remain available.
- Explicit error/failed events remain failures. ChatABC treats done only as an
  end marker, as in customer workflow&bianpai.py: its payload is not interpreted
  as another business-success check. Yunxia follows customer yunxia.py by ignoring
  done without stopping event collection; neither done.status nor message.ok/status
  adds a failure condition. Explicit errors are not cleared by later messages,
  and trace events are still collected after done or an error.
  Missing done, non-JSON auxiliary events, duplicate workflow end messages
  and empty answer text no longer receive extra virtual-bank parser restrictions.
  Workflow uses the first nonempty end-node answer, as in the customer code; empty
  text is left for the evaluator rather than treated as an SSE protocol violation.
- Unified deletion payloads and the prior per-adapter deletion-success predicates
  remain unchanged, as requested. This work does not resolve that predicate difference.

## Trace and evaluation ownership

The adapters still retrieve real evidence through the existing TraceServerClient
and normalize it into AgentGate Trace objects. Chat's raw SSE trace events are passed
to that existing reference boundary. Missing/malformed references, inaccessible
Trace Server, or invalid evidence still fail; answers are not substituted for real
process traces. The customer's debugTrace flag alone does not promise the required
project_id/trace_id reference format.

Readiness polling lives only in `chatabc.py` and `yunxia.py`; `trace_server.py`
is unchanged. Each Turn queries immediately and then waits up to two seconds
between attempts when HTTP 404/409/429, transport timeout/unavailability, or
recognizably pending evidence is returned. Pending evidence includes a running
root/span, missing successful-root output/duration, or fewer spans than declared.
The existing minimum of Case timeout and request timeout is the polling budget;
the client's two sequential HTTP requests each receive half the remaining budget.
Success stops polling immediately without replaying a chat. Authorization errors,
invalid evidence and identity mismatches still fail without readiness retries.
Exhausting the budget still fails and preserves Pod cleanup. A missing Trace
reference cannot be polled. Output-only fallback has been proposed but is not
implemented in this checkpoint.

The server does not expose a final LLM-request count/completeness watermark here.
These checks handle recognized pending states, not a guarantee that every later
LLM/observation upload has arrived. HTTP socket timeouts and the polling budget
are not a hard wall-clock cancellation of an in-flight response.

RunEngine still owns Case execution, failure state, evaluator invocation, persistence
and reports. Its current fail-fast behavior and the in-bank serial/no-Case-retry
restriction are retained. Customer Excel iteration, DB status updates, thread pools,
scoring endpoints, result workbooks and whole-task main functions are not imported.
File upload and VIP/reset-key flows are not exposed by this text-only integration.

Real in-bank adapters no longer call `bank_protocol.parse_bank_sse`. The shared
virtual-bank parser, local virtual Agents, front-end, RunEngine, evaluator, storage
and report contracts are not changed by this adaptation.

## Installation and validation

`requests` is now a declared dependency in pyproject.toml and uv.lock. An offline
deployment must include requests and its locked dependencies (including urllib3 and
charset-normalizer), not just copy the two Python files. No customer-platform calls
or Pod operations are executed by the local tests.

Focused checks:

```bash
PYTHONPATH=src python -m pytest \
  tests/test_inbank_customer_contract.py \
  tests/test_target_execution.py \
  tests/test_target_execution_factory.py \
  tests/test_inbank_trace_server_adapters.py \
  tests/test_bank_protocol.py tests/test_run_engine.py \
  tests/test_local_bank_adapter.py tests/test_demo_loan_target.py \
  tests/test_trace_server_client.py -q
```

Coverage includes three adapter types, multiple Cases/Turns, persisted Worker/Celery
entry points, evaluator results/report, real loopback HTTP/SSE and Trace Server
queries, exact request identity, retries and early success, health timeout cleanup,
stream cleanup, explicit errors, auxiliary events and cross-Case customer isolation.

Verification on 2026-09-23: focused suite **269 passed, 26 skipped**; full suite
**1483 passed, 52 skipped, 8 failed** (BJS environment failures explained below).
After adapter-side readiness polling: focused suite **317 passed, 26 skipped**,
including real loopback HTTP 404-then-ready responses for all three Agent types,
early success, deadline exhaustion, permanent rejection and unchanged chat counts.
The full suite was not rerun for this readiness-only checkpoint.
The two adapter modules and new customer contract tests pass Ruff lint/format checks;
the dependency lock and diff whitespace checks also pass.

An additional one-off AST-isolated comparison against the supplied original scripts
passed 51 assertions for lifecycle wire requests, session variable mapping, SSE
frames, answer extraction and Yunxia payloads. It executes only selected functions,
never customer DB/main code. Portable regression fixtures live in the tests above.

The full local suite also exercises unrelated launchers: eight BJS process tests
currently fail because this workspace has no `.venv/bin/python`. The same eight
failures reproduce with the pre-change adapter sources. They are not marked passed
or skipped, and the launcher is not changed as part of this work. Customer-environment
acceptance, including Trace reference availability, remains required.
