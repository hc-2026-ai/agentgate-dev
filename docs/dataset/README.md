# Dataset and Case

Implementation sequence: [implementation-plan.md](implementation-plan.md).
Current capability status: [project progress](../project-progress.md).

Dataset and Case ownership is split by layer:

```text
domain/case.py                  Individual Case and multi-turn conversation models
domain/dataset.py               Dataset aggregate, versions, and collection invariants
dataset/                        Reusable loading, export, versioning, sampling,
                                generation, and format mechanics
application/dataset_management.py
                                User-facing Dataset/Case lifecycle orchestration
storage/                        Persistence implementations
server/ and cli/                Transport entry points
```

The pre-refactor implementation plan is archived under
[planning history](../history/planning-v1/dataset-implementation-plan.md). It retains
behavior and acceptance criteria but is not implementation authority.

Automatic generation is a separate application use case that coordinates Target metadata,
`dataset/generation/`, a model provider, and Dataset draft creation.

## Sample initialization UI

The sample editor's **初始变量** section uses one variable table for base
orchestration and workflow agents, without a manual type selector. Base variables
use `initial_state.prompt_variables`; workflow variables use
`initial_state.config_variables`. Existing samples select their saved field
automatically, preferring `config_variables` when both fields exist. Published
samples remain read-only, and other imported initial-state fields are preserved.

Manually added variables default to text (`type: "str"`), displayed as **str**.
Their required status is **-** until retrieved from the selected agent; no
`required` field is invented or required for import/save. Existing imported entries
without type metadata remain unchanged. Completely blank new rows are still ignored.

The agent, branch, and version selectors reuse the evaluation task's
`AgentTargetPicker`. External login uses `localAgentDirectory` and the `local`
token; in-bank login uses `agentDirectory` with the current session token and
team. Selection is temporary editor state and does not change the sample schema.
Standalone in-bank login and embedded integrated login share the in-bank task
input checks; the external platform mock retains its text-only restriction.

**自动获取变量** queries the selected agent's exact `agentId` and `agentVersion`
through `/web/agent/getAgentConfigJsonObjectByAgentIdAndVersion`. The existing
directory provider supplies the origin and token for the active login mode. For
base orchestration (`agent_type: "tool_agent"`), read `agent_config.prompt_variables`
and use each variable's `key` as its initialization `name`; text type `string`
becomes `str`. For workflows, read `config.variables` from nodes whose `type` is
`Start` and use `name`. Exclude `BOT_USER_INPUT` and `user_input` only from the
automatically fetched definitions. Manually entered,
imported, or existing saved variables are displayed and saved unchanged. Each
turn's user input continues to be sent through `chat.txt`, as in the customer
script. The backend client keeps its existing initialization behavior, including
the base `tool_variables` payload. No tool-variable editor or file upload is added.
Display type and required metadata without adding required-value checks or
additional handling of `default`, `scope`, or `readonly`. Newly fetched base
variables save only `name`, `value`, and `type`; required status is a fetch-time
display hint because the base client forwards variable entries unchanged.
Workflow required metadata continues to be saved as before.
Returned definitions replace the current variable list; matching values are
retained, including file variable values. File variables (`image`, `doc`, `audio`)
can be filled in as initialization values, but there is no file picker, local file
access, or upload. A successful fetch switches the saved variable field to match
the selected agent and removes the previous base/workflow variable field when
converting formats. Selecting another query agent does not replace nonempty
edits before fetching successfully; an empty table follows the selected type for
manual entry. Before the in-bank workflow `init_session`
request, the existing ChatABC client forwards only each variable's `name`, `value`,
and `type`, matching the customer script's variable entries. `required` remains
editor metadata in the saved Case; it is not sent to the customer. Missing optional
`type` is not filled in and nonempty values are not coerced. As in the customer
script's variable collection, nulls and blank/whitespace-only strings are omitted
from the outgoing initialization list. Their definitions and required markers
remain in the saved Case and editor; this does not add required-value validation.
Valid `0` and `false` values are retained.
File variable values are forwarded to `init_session` in the same way as other
nonempty initialization values. This does not guarantee the customer will accept
initialization or successfully run nodes that need the actual files. Chat keeps
the existing `files: []` behavior; nonempty per-turn attachments remain unsupported.
There is no database change or file upload.

Failed queries leave the current form unchanged and allow retry or manual editing.
Responses belonging to a previous selection, login context, or sample are ignored.
Variable retrieval and sample saving cannot overlap: both the editor's save button
and the manual-create wizard's footer wait for retrieval to finish; saving disables
retrieval until the saved sample has been refreshed.
Yunxia variable retrieval remains outside this scope. External queries require
the configured local mock to provide the same
configuration endpoint; this change does not add a route to that service. Manual
editing does not require selecting an agent.

Focused acceptance coverage is in
`frontend/tests/dataset-sample-editor.spec.ts`, including both directory routes,
selection invalidation, query errors/retries, stale responses, file placeholders,
persistence, and read-only values. Browser routes simulate customer responses;
live in-bank acceptance remains necessary.
