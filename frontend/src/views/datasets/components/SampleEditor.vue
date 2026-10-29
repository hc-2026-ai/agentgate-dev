<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import type { EvaluationCase, ValidationIssue, Expectation, JsonObject } from '../types/index';
import { blankSample } from '../utils/sample-import';
import { outputEditorValue, outputCondition } from '../utils/sample-output';
import JsonCodeEditor from '../../../components/JsonCodeEditor.vue';
import ExpectationEditor from './ExpectationEditor.vue';
import AgentTargetPicker, {
  type AgentTargetSelection,
} from '../../evaluation/components/AgentTargetPicker.vue';
import { agentDirectory, localAgentDirectory } from '../../../api/agent-platform';
import { useAuthStore } from '../../../stores/modules/auth';
const props = defineProps<{
  item: EvaluationCase | null;
  editable: boolean;
  saving?: boolean;
  persisted?: boolean;
  hideSaveButton?: boolean;
  validationIssues?: ValidationIssue[];
}>();
const emit = defineEmits<{ save: [item: EvaluationCase]; dirtyChange: [dirty: boolean] }>();
const auth = useAuthStore();
const platformDirectory = computed(() =>
  auth.usesPlatformDirectory ? agentDirectory : localAgentDirectory,
);
const platformToken = computed(() => (auth.usesPlatformDirectory ? auth.token : 'local'));
const platformTeamId = computed(() => (auth.usesPlatformDirectory ? auth.teamId : ''));
const variableAgent = ref<AgentTargetSelection | null>(null);
const variableFetchNotice = ref('');
const fetchingVariables = ref(false);
const selectedInitializationField = computed(() => initializationFieldForAgent(variableAgent.value));
let variableFetchSequence = 0;
watch(
  [
    variableAgent,
    platformDirectory,
    platformToken,
    platformTeamId,
    () => props.item,
    () => props.editable,
  ],
  () => {
    variableFetchSequence++;
    fetchingVariables.value = false;
    variableFetchNotice.value = '';
  },
  { flush: 'sync' },
);
onBeforeUnmount(() => variableFetchSequence++);
function receiveVariableAgent(selection: AgentTargetSelection | null) {
  variableAgent.value = selection;
  const field = initializationFieldForAgent(selection);
  if (
    field &&
    !configVariables.value[initializationField.value].length &&
    configVariablesRaw.value[initializationField.value] === null
  )
    initializationField.value = field;
}
async function requestVariables() {
  const selection = variableAgent.value;
  const field = selectedInitializationField.value;
  const getVariables = platformDirectory.value.getInitializationVariables;
  if (
    !props.editable ||
    props.saving ||
    !selection ||
    !field ||
    fetchingVariables.value ||
    !getVariables
  )
    return;
  const ticket = ++variableFetchSequence;
  fetchingVariables.value = true;
  variableFetchNotice.value = '';
  try {
    const variables = await getVariables({
      token: platformToken.value,
      agentId: selection.agentId,
      agentVersion: selection.agentVersion,
    });
    if (ticket !== variableFetchSequence) return;
    const existing = configVariables.value[initializationField.value];
    configVariables.value[field] = variables.map((variable) => {
      const previous = existing.find((row) => row.name === variable.name);
      const extra: JsonObject = { type: variable.type };
      if (field === 'config_variables') extra.required = variable.required;
      return {
        id: previous?.id ?? crypto.randomUUID(),
        name: variable.name,
        value: previous?.value ?? '',
        format: previous?.format ?? 'text',
        extra,
        requiredHint: variable.required,
      };
    });
    configVariablesRaw.value[field] = null;
    initializationField.value = field;
    variableFetchNotice.value = variables.length
      ? `已获取 ${variables.length} 个初始化变量，请填写变量值。`
      : '该智能体没有初始化变量。';
  } catch (cause) {
    if (ticket !== variableFetchSequence) return;
    const status = (cause as { status?: number } | null)?.status;
    variableFetchNotice.value =
      status === 401 || status === 403
        ? '获取变量失败：Token 无效或无权访问，请重新登录。已有内容已保留。'
        : '获取变量失败，请稍后重试或手动填写。已有内容已保留。';
  } finally {
    if (ticket === variableFetchSequence) fetchingVariables.value = false;
  }
}
const form = ref<EvaluationCase | null>(null),
  index = ref(0),
  error = ref(''),
  query = ref('');
const requiredTools = ref<string[]>([]),
  forbiddenTools = ref<string[]>([]);
const inputs = ref<string[]>([]),
  inputJson = ref<boolean[]>([]),
  outputs = ref<string[]>([]),
  outputJson = ref<boolean[]>([]);
type ConfigVariableRow = {
  id: string;
  name: string;
  value: string;
  format: 'text' | 'json';
  extra: JsonObject;
  requiredHint?: boolean;
};
type InitializationField = 'config_variables' | 'prompt_variables';
const initializationField = ref<InitializationField>('config_variables');
const initializationGroups = computed(() => [
  { key: initializationField.value, label: '初始化变量' },
]);
const configVariables = ref<Record<InitializationField, ConfigVariableRow[]>>({
  config_variables: [],
  prompt_variables: [],
});
const configVariablesRaw = ref<Record<InitializationField, string | null>>({
  config_variables: null,
  prompt_variables: null,
});
function initializationFieldForAgent(selection: AgentTargetSelection | null): InitializationField | null {
  const type = selection?.platformArrangeType || selection?.platformAgentType;
  return type === 'base' ? 'prompt_variables' : type === 'workflow' ? 'config_variables' : null;
}
function initializationFieldForState(initial: JsonObject): InitializationField {
  return 'config_variables' in initial || !('prompt_variables' in initial)
    ? 'config_variables'
    : 'prompt_variables';
}
function requiredHint(variable: ConfigVariableRow) {
  const required = variable.requiredHint ?? variable.extra.required;
  return typeof required === 'boolean' ? (required ? '是' : '否') : '-';
}
type OutputRule = Extract<Expectation, { kind: 'output' }>;
const outputRules = ref<OutputRule[]>([]);
const originalOutputs = ref<{ text: string; json: boolean }[]>([]);
const ruleEditor = ref<{ validate: () => void } | null>(null);
const ruleDraftDirty = ref(false);
const editorRevision = ref(0);
const baseline = ref(''),
  inputField = ref<'query' | 'txt'>('txt');
const turn = computed(() => form.value?.turns[index.value]);
const basicOutput = computed({
  get: () => outputs.value[index.value],
  set: (value: string) => {
    outputs.value[index.value] = value;
    syncBasicOutput();
  },
});
const basicOutputJson = computed({
  get: () => outputJson.value[index.value],
  set: (value: boolean) => {
    outputJson.value[index.value] = value;
    syncBasicOutput();
  },
});
function snapshot() {
  return JSON.stringify([
    form.value,
    inputs.value,
    inputJson.value,
    outputs.value,
    outputJson.value,
    outputRules.value,
    inputField.value,
    requiredTools.value,
    forbiddenTools.value,
    configVariables.value,
    configVariablesRaw.value,
    initializationField.value,
  ]);
}
const dirty = computed(() => ruleDraftDirty.value || snapshot() !== baseline.value);
watch(dirty, (v) => emit('dirtyChange', v));
watch(
  () => props.item,
  (item) => {
    editorRevision.value++;
    inputField.value = item?.turns.some((t) => 'txt' in t.input)
      ? 'txt'
      : item?.turns.some((t) => String(t.input.query ?? '').trim())
        ? 'query'
        : 'txt';
    requiredTools.value = item?.turns.map((t) => t.required_tools.join('，')) ?? [];
    forbiddenTools.value = item?.turns.map((t) => t.forbidden_tools.join('，')) ?? [];
    form.value = item ? JSON.parse(JSON.stringify(item)) : null;
    const initial = item?.initial_state ?? {};
    initializationField.value = initializationFieldForState(initial);
    loadConfigVariables('config_variables', initial.config_variables);
    loadConfigVariables('prompt_variables', initial.prompt_variables);
    index.value = 0;
    error.value = '';
    ruleDraftDirty.value = false;
    inputJson.value =
      item?.turns.map(
        (t) =>
          !(
            Object.keys(t.input).length === 1 && typeof (t.input.query ?? t.input.txt) === 'string'
          ),
      ) ?? [];
    inputs.value =
      item?.turns.map((t, i) =>
        inputJson.value[i]
          ? JSON.stringify(t.input, null, 2)
          : String(t.input.query ?? t.input.txt),
      ) ?? [];
    outputRules.value =
      form.value?.turns.map((t) => {
        const primary = t.expectations.find(
          (e): e is OutputRule =>
            e.kind === 'output' && e.path === null && e.condition.kind === 'equals',
        );
        return primary ? JSON.parse(JSON.stringify(primary)) : emptyOutputRule();
      }) ?? [];
    const values = outputRules.value.map((r) => outputEditorValue(r.condition));
    originalOutputs.value = values.map((v) => ({ ...v }));
    outputs.value = values.map((v) => v.text);
    outputJson.value = values.map((v) => v.json);
    baseline.value = snapshot();
    emit('dirtyChange', false);
  },
  { immediate: true },
);
function loadConfigVariables(key: InitializationField, value: unknown) {
  const variables = value === undefined ? [] : value;
  configVariables.value[key] = [];
  configVariablesRaw.value[key] = null;
  if (
    !Array.isArray(variables) ||
    variables.some(
      (variable) =>
        !variable ||
        typeof variable !== 'object' ||
        Array.isArray(variable) ||
        typeof variable.name !== 'string' ||
        !('value' in variable),
    )
  ) {
    // Keep malformed imported data visible and editable instead of dropping it.
    configVariablesRaw.value[key] = JSON.stringify(variables, null, 2);
    return;
  }
  configVariables.value[key] = variables.map(({ name, value, ...extra }) => ({
    id: crypto.randomUUID(),
    name,
    value: typeof value === 'string' ? value : JSON.stringify(value),
    format: typeof value === 'string' ? 'text' : 'json',
    extra,
  }));
}
function addConfigVariable(key: InitializationField) {
  configVariables.value[key].push({
    id: crypto.randomUUID(),
    name: '',
    value: '',
    format: 'text',
    extra: { type: 'str' },
  });
}
function emptyOutputRule(): Extract<Expectation, { kind: 'output' }> {
  return {
    id: crypto.randomUUID(),
    kind: 'output',
    name: '人工期望输出',
    path: null,
    condition: { kind: 'equals', expected: '' },
  };
}
function syncBasicOutput() {
  if (!turn.value) return;
  const i = index.value;
  try {
    const rule = outputRules.value[i];
    const condition = outputCondition(outputs.value[i], outputJson.value[i], rule.condition);
    const position = turn.value.expectations.findIndex((e) => e.id === rule.id);
    if (condition) {
      const updated = { ...rule, condition };
      if (position < 0) turn.value.expectations.push(updated);
      else turn.value.expectations.splice(position, 1, updated);
      outputRules.value[i] = JSON.parse(JSON.stringify(updated));
    } else if (position >= 0) turn.value.expectations.splice(position, 1);
    originalOutputs.value[i] = { text: outputs.value[i], json: outputJson.value[i] };
  } catch {
    // Keep the basic field's invalid JSON draft visible; save reports its parse error.
  }
}
function validateRules() {
  try {
    ruleEditor.value?.validate();
    error.value = '';
    return true;
  } catch (e) {
    error.value = String(e);
    return false;
  }
}
function selectTurn(nextIndex: number) {
  if (nextIndex === index.value || !validateRules()) return;
  index.value = nextIndex;
}
function add() {
  if (!validateRules()) return;
  form.value!.turns.push(blankSample().turns[0]);
  inputs.value.push('');
  inputJson.value.push(false);
  outputs.value.push('');
  outputJson.value.push(false);
  const rule = emptyOutputRule();
  outputRules.value.push(rule);
  originalOutputs.value.push({ text: '', json: false });
  requiredTools.value.push('');
  forbiddenTools.value.push('');
  index.value = form.value!.turns.length - 1;
}
function remove() {
  if (!validateRules()) return;
  if (!form.value || form.value.turns.length < 2) return;
  form.value.turns.splice(index.value, 1);
  for (const list of [
    inputs.value,
    inputJson.value,
    outputs.value,
    outputJson.value,
    outputRules.value,
    originalOutputs.value,
    requiredTools.value,
    forbiddenTools.value,
  ])
    list.splice(index.value, 1);
  index.value = Math.max(0, index.value - 1);
}
function save() {
  if (props.saving || fetchingVariables.value) return;
  error.value = '';
  if (!validateRules()) return;
  if (!form.value?.name.trim()) {
    error.value = '请输入样本名称';
    return;
  }
  try {
    const item: EvaluationCase = JSON.parse(JSON.stringify(form.value));
    for (const { key } of initializationGroups.value) {
      const variables =
        configVariablesRaw.value[key] !== null
          ? JSON.parse(configVariablesRaw.value[key]!)
          : configVariables.value[key].flatMap((variable, i) => {
              if (
                !variable.name.trim() &&
                variable.value === '' &&
                Object.entries(variable.extra).every(
                  ([key, value]) => key === 'type' && value === 'str',
                )
              )
                return [];
              let value: unknown = variable.value;
              if (variable.format === 'json') {
                try {
                  value = JSON.parse(variable.value);
                } catch {
                  throw Error(`第 ${i + 1} 项初始化变量的值不是有效 JSON`);
                }
              }
              return [{ ...variable.extra, name: variable.name, value }];
            });
      if (!Array.isArray(variables)) throw Error('初始化变量必须为 JSON 数组');
      const names = new Set<string>();
      for (const variable of variables) {
        if (
          !variable ||
          typeof variable !== 'object' ||
          Array.isArray(variable) ||
          typeof variable.name !== 'string' ||
          !variable.name.trim() ||
          !('value' in variable)
        )
          throw Error('初始化变量需填写变量名，并包含值字段');
        if (names.has(variable.name)) throw Error(`初始化变量名“${variable.name}”不能重复`);
        names.add(variable.name);
      }
      const current = item.initial_state[key];
      if (variables.length) item.initial_state[key] = variables;
      else if (!Array.isArray(current) || current.length) delete item.initial_state[key];
    }
    if (initializationField.value !== initializationFieldForState(form.value.initial_state))
      delete item.initial_state[
        initializationField.value === 'prompt_variables' ? 'config_variables' : 'prompt_variables'
      ];
    item.turns.forEach((t, i) => {
      if (!inputs.value[i].trim()) throw Error('第 ' + (i + 1) + ' 轮用户输入不能为空');
      t.input = inputJson.value[i]
        ? JSON.parse(inputs.value[i])
        : { [inputField.value]: inputs.value[i] };
      if (!t.input || typeof t.input !== 'object' || Array.isArray(t.input))
        throw Error('用户输入 JSON 必须为对象');
      if (!Object.keys(t.input).length) throw Error('用户输入 JSON 不能为空对象');
      t.required_tools = requiredTools.value[i]
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean);
      t.forbidden_tools = forbiddenTools.value[i]
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean);
      const rule = outputRules.value[i];
      const unchanged =
        outputs.value[i] === originalOutputs.value[i].text &&
        outputJson.value[i] === originalOutputs.value[i].json;
      const position = t.expectations.findIndex((e) => e.id === rule.id);
      const condition =
        unchanged && position >= 0
          ? rule.condition
          : outputCondition(outputs.value[i], outputJson.value[i], rule.condition);
      if (condition) {
        if (position >= 0) t.expectations.splice(position, 1, { ...rule, condition });
        else t.expectations.push({ ...rule, condition });
      } else if (position >= 0) t.expectations.splice(position, 1);
    });
    emit('save', item);
  } catch (e) {
    error.value = '保存失败：' + String(e);
  }
}
function updateExpectations(values: Expectation[]) {
  if (!turn.value) return;
  turn.value.expectations = values;
  const i = index.value;
  const previous = outputRules.value[i];
  const candidates = values.filter(
    (e): e is OutputRule => e.kind === 'output' && e.path === null && e.condition.kind === 'equals',
  );
  const primary =
    candidates.find((e) => e.id === previous.id) ?? candidates[0] ?? emptyOutputRule();
  if (
    primary.id !== previous.id ||
    JSON.stringify(primary.condition) !== JSON.stringify(previous.condition)
  ) {
    const value = outputEditorValue(primary.condition);
    outputs.value[i] = value.text;
    outputJson.value[i] = value.json;
    originalOutputs.value[i] = value;
  }
  outputRules.value[i] = JSON.parse(JSON.stringify(primary));
}
defineExpose({ save, fetchingVariables });
</script>
<template>
  <section class="sample-editor">
    <p v-if="!form" class="empty">选择数据样本，或点击新增样本。</p>
    <template v-else>
      <div class="sample-meta">
        <label
          >样本名称<input v-model="form.name" :disabled="!editable" aria-label="样本名称"
        /></label>
        <label
          title="正例：应正常完成；负例：应正确拒绝或处理异常；边界：临界条件。分类不会直接决定测评通过与否。"
          >分类
          <select v-model="form.category" :disabled="!editable" aria-label="样本分类">
            <option value="positive">正例</option>
            <option value="negative">负例</option>
            <option value="boundary">边界</option>
          </select>
        </label>
      </div>
      <details :key="form.id" class="case-initialization" aria-label="初始变量">
        <summary>初始变量</summary>
        <div class="variable-agent-picker">
          <p>数据源：{{ auth.modeLabel }}</p>
          <AgentTargetPicker
            :directory="platformDirectory"
            :token="platformToken"
            :team-id="platformTeamId"
            :disabled="!editable"
            @selection-change="receiveVariableAgent"
          />
        </div>
        <div v-for="group in initializationGroups" :key="group.key" class="initialization-group">
          <div class="initialization-heading">
            <button
              v-if="editable"
              class="add-variable fetch-variables"
              type="button"
              :disabled="!selectedInitializationField || fetchingVariables || saving"
              :aria-busy="fetchingVariables"
              :title="
                !variableAgent
                  ? '请先选择智能体及版本'
                  : !selectedInitializationField
                    ? '当前仅支持获取基础编排和工作流变量'
                    : '获取所选智能体版本的初始化变量'
              "
              @click="requestVariables"
            >
              {{ fetchingVariables ? '获取中…' : '自动获取变量' }}
            </button>
            <button
              v-if="editable && configVariablesRaw[group.key] === null"
              class="add-variable"
              type="button"
              @click="addConfigVariable(group.key)"
              aria-label="添加变量"
            >
              <span aria-hidden="true">＋</span> 添加变量
            </button>
          </div>
          <p v-if="!variableAgent && !fetchingVariables">
            请先选择智能体及版本，再点击“自动获取变量”。
          </p>
          <p v-if="variableAgent && !selectedInitializationField">当前仅支持自动获取基础编排和工作流的初始化变量。</p>
          <p v-if="variableFetchNotice" role="status">{{ variableFetchNotice }}</p>
          <template v-if="configVariablesRaw[group.key] !== null">
            <p class="initialization-warning">已有初始化配置格式不正确，请修正后保存。</p>
            <JsonCodeEditor
              :model-value="configVariablesRaw[group.key]!"
              :disabled="!editable"
              :label="`${group.label}原始 JSON`"
              @update:model-value="configVariablesRaw[group.key] = $event"
            />
          </template>
          <div v-else-if="configVariables[group.key].length" class="variable-table">
            <div class="variable-columns" aria-hidden="true">
              <span>变量名 · Key</span><span>类型</span><span>是否必填</span>
              <span>值 · Value</span><span />
            </div>
            <div
              v-for="(variable, i) in configVariables[group.key]"
              :key="variable.id"
              class="variable-row"
            >
              <input
                v-model="variable.name"
                :disabled="!editable"
                :aria-label="`${group.label}名 ${i + 1}`"
                placeholder="如 centreId"
                autocomplete="off"
                spellcheck="false"
              />
              <span class="variable-metadata" :aria-label="`${group.label}类型 ${i + 1}`">
                {{ variable.extra.type || '—' }}
              </span>
              <span class="variable-metadata" :aria-label="`${group.label}是否必填 ${i + 1}`">
                {{ requiredHint(variable) }}
              </span>
              <div class="variable-value">
                <textarea
                  v-if="variable.format === 'json'"
                  v-model="variable.value"
                  :disabled="!editable"
                  :aria-label="`${group.label}值 ${i + 1}`"
                  rows="2"
                  placeholder="填写 JSON 值"
                  spellcheck="false"
                />
                <input
                  v-else
                  v-model="variable.value"
                  :disabled="!editable"
                  :aria-label="`${group.label}值 ${i + 1}`"
                  placeholder="填写变量值"
                  autocomplete="off"
                />
              </div>
              <button
                v-if="editable"
                class="remove-variable"
                type="button"
                :aria-label="`删除${group.label} ${i + 1}`"
                title="删除变量"
                @click="configVariables[group.key].splice(i, 1)"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true" width="16" height="16">
                  <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7m4-7v7" />
                </svg>
              </button>
            </div>
          </div>
          <p v-else class="initialization-empty">暂无初始化变量，无需配置时可留空。</p>
        </div>
      </details>
      <div class="conversation-layout">
        <aside>
          <div class="round-title">
            <b>轮次列表（{{ form.turns.length }}）</b>
          </div>
          <input v-model="query" aria-label="搜索轮次内容" placeholder="搜索轮次内容" /><button
            v-for="(t, i) in form.turns"
            v-show="!query || inputs[i].includes(query)"
            :key="t.id"
            class="round"
            :class="{ active: index === i }"
            @click="selectTurn(i)"
          >
            <b>第 {{ i + 1 }} 轮</b><small>{{ inputs[i] || '未填写内容' }}</small></button
          ><button v-if="editable" class="add-round" @click="add">＋ 添加新轮次</button>
        </aside>
        <div v-if="turn" class="turn-content">
          <section class="evaluation-section" aria-label="基本测评项">
            <h3>基本测评项</h3>
            <div class="basic-messages">
              <div class="message-card user">
                <header>
                  <b>用户输入 <em class="required">*</em></b
                  ><span>{{ inputs[index].length }} 字</span>
                </header>
                <label
                  ><input type="checkbox" v-model="inputJson[index]" :disabled="!editable" />结构化
                  JSON 输入</label
                ><JsonCodeEditor
                  v-if="inputJson[index]"
                  :key="turn.id + 'input'"
                  :model-value="inputs[index]"
                  :disabled="!editable"
                  label="用户输入 JSON"
                  @update:model-value="inputs[index] = $event"
                /><textarea
                  v-else
                  v-model="inputs[index]"
                  :disabled="!editable"
                  rows="5"
                  aria-label="用户输入"
                  aria-required="true"
                  required
                  placeholder="请输入用户提问内容"
                />
              </div>
              <div class="message-card expected">
                <header>
                  <b>期望输出</b><span>{{ outputs[index].length }} 字</span>
                </header>
                <label
                  ><input type="checkbox" v-model="basicOutputJson" :disabled="!editable" />结构化
                  JSON 期望</label
                ><JsonCodeEditor
                  v-if="outputJson[index]"
                  :key="turn.id + 'output'"
                  :model-value="basicOutput"
                  :disabled="!editable"
                  label="期望输出 JSON"
                  @update:model-value="basicOutput = $event"
                /><textarea
                  v-else
                  v-model="basicOutput"
                  :disabled="!editable"
                  rows="5"
                  aria-label="期望输出"
                  placeholder="人工确认的期望输出，不自动复制实际回答"
                />
              </div>
            </div>
          </section>
          <section class="evaluation-section" aria-label="高级测评项">
            <h3>高级测评项</h3>
            <div class="message-card tools">
              <header><b>期望工具调用</b></header>
              <label
                >必需工具（逗号分隔）<input
                  v-model="requiredTools[index]"
                  :disabled="!editable" /></label
              ><label
                >禁止工具（逗号分隔）<input v-model="forbiddenTools[index]" :disabled="!editable"
              /></label>
            </div>
            <div class="message-card tools">
              <header><b>期望 Skill 调用</b></header>
              <label
                >期望 Skill<input
                  class="skill-input"
                  v-model="turn.expected_skill"
                  :disabled="!editable"
              /></label>
            </div>
            <div class="advanced">
              <ExpectationEditor
                :key="`${editorRevision}:${turn.id}`"
                ref="ruleEditor"
                @draft-change="ruleDraftDirty = $event"
                :model-value="turn.expectations"
                :disabled="!editable"
                @update:model-value="updateExpectations($event)"
              />
              <details v-if="turn.original_expectations?.some((e) => e.kind === 'execution_path')">
                <summary>执行路径与 Skill 检查（保存时保留）</summary>
                <pre style="white-space: pre-wrap">{{
                  JSON.stringify(
                    turn.original_expectations.filter((e) => e.kind === 'execution_path'),
                    null,
                    2,
                  )
                }}</pre>
              </details>
            </div>
          </section>
          <button v-if="editable && form.turns.length > 1" class="remove-round" @click="remove">
            删除当前轮次（保存后生效）
          </button>
        </div>
      </div>
      <div class="editor-footer">
        <span>{{
          editable
            ? dirty || !persisted
              ? '● 有未保存的修改'
              : '已保存到数据库草稿'
            : '已发布版本 · 只读'
        }}</span
        ><button
          v-if="editable && !hideSaveButton"
          class="primary"
          :disabled="saving || fetchingVariables"
          :title="fetchingVariables ? '正在获取初始化变量，请稍候' : ''"
          @click="save"
        >
          {{ saving ? '保存中…' : '保存样本' }}
        </button>
      </div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-for="issue in validationIssues" :key="issue.path" class="error">
        {{ issue.path }}：{{ issue.message }}
      </p>
    </template>
  </section>
</template>
<style scoped>
.sample-editor {
  background: #fff;
  border: 1px solid #e0e6f0;
  border-radius: 12px;
  overflow: hidden;
  min-width: 0;
  color: #34435a;
}
.sample-meta {
  padding: 18px;
  display: flex;
  gap: 16px;
  border-bottom: 1px solid #e1e7f1;
}
.sample-meta label {
  flex: 1;
  min-width: 0;
}
.sample-editor .case-initialization {
  margin: 0;
  padding: 16px 18px;
  border-bottom: 1px solid #e1e7f1;
}
.variable-agent-picker {
  margin-top: 16px;
}
.initialization-type {
  display: inline-flex;
  border: 1px solid #dbe2ee;
  border-radius: 6px;
  overflow: hidden;
}
.initialization-type button {
  border: 0;
  padding: 6px 14px;
  background: #fff;
  color: #70829a;
  font: inherit;
  cursor: pointer;
}
.initialization-type button[aria-pressed='true'] {
  background: #eaf7f2;
  color: #008b76;
}
.initialization-type button:disabled {
  cursor: default;
}
.initialization-heading {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin: 12px 0;
}
.initialization-heading > span {
  margin-right: auto;
}
.case-initialization[open] > summary {
  margin-bottom: 12px;
}
.case-initialization p {
  color: #74829a;
  font-size: 12px;
  line-height: 1.6;
}
.case-initialization .initialization-empty {
  padding: 12px;
  margin: 0;
  border: 1px dashed #dbe2ee;
  border-radius: 7px;
  background: #fafbfd;
  text-align: center;
}
.case-initialization .initialization-warning {
  color: #b45309;
}
.add-variable {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 5px 10px;
  border: 1px solid #cde3dc;
  border-radius: 6px;
  background: #fff;
  color: #008b76;
  font-size: 12px;
  cursor: pointer;
}
.add-variable:hover {
  background: #edf7f4;
}
.fetch-variables:disabled {
  color: #93a0b3;
  background: #f8fafc;
  border-color: #dbe2ee;
  cursor: not-allowed;
}
.variable-columns,
.variable-row {
  display: grid;
  grid-template-columns: minmax(100px, 1fr) 64px 64px minmax(140px, 2fr) 28px;
  gap: 10px;
  align-items: start;
}
.variable-columns {
  padding-bottom: 7px;
  color: #74829a;
  font-size: 12px;
}
.variable-row + .variable-row {
  margin-top: 8px;
}
.variable-value {
  min-width: 0;
}
.variable-metadata {
  padding: 8px 0;
  color: #52617b;
  font-size: 13px;
  line-height: 18px;
  overflow-wrap: anywhere;
}
.sample-editor .variable-row input,
.sample-editor .variable-row textarea {
  min-width: 0;
  margin-top: 0;
  padding: 8px 10px;
  font-size: 13px;
  line-height: 18px;
}
.variable-value textarea {
  resize: vertical;
}
.variable-row input:focus,
.variable-row textarea:focus {
  outline: 2px solid #d0eee5;
  outline-offset: 1px;
  border-color: #008b76;
}
.variable-row input:disabled,
.variable-row textarea:disabled {
  background: #f8fafc;
  color: #52617b;
}
.remove-variable {
  display: grid;
  place-items: center;
  height: 36px;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: #8b97a8;
  cursor: pointer;
}
.remove-variable:hover {
  color: #c55;
  background: #fff1f0;
}
.remove-variable svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.sample-editor label {
  display: block;
  font-size: 12px;
  color: #74829a;
  margin: 8px 0;
}
.sample-editor input:not([type='checkbox']),
.sample-editor select,
.sample-editor textarea {
  box-sizing: border-box;
  width: 100%;
  border: 1px solid #dbe2ee;
  border-radius: 7px;
  background: #fff;
  padding: 10px;
  margin-top: 7px;
  color: #34435a;
  font: inherit;
}
.sample-meta input,
.sample-meta select,
.sample-editor .skill-input {
  height: 40px;
}
.required {
  color: var(--el-color-danger);
  font-style: normal;
}
.sample-editor textarea:disabled {
  background: #f8fafc;
}
.conversation-layout {
  display: grid;
  grid-template-columns: 210px minmax(0, 1fr);
}
aside {
  padding: 16px;
  background: #f6f8fc;
  border-right: 1px solid #e1e7f1;
}
.round-title {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
}
.round {
  display: block;
  width: 100%;
  text-align: left;
  border: 0;
  border-radius: 8px;
  background: transparent;
  padding: 16px;
  margin: 8px 0;
  color: #52617b;
}
.round.active {
  background: #eaf7f2;
  color: #008b76;
}
.round small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 6px;
}
.add-round {
  width: 100%;
  border: 1px dashed #bacbe4;
  background: white;
  padding: 12px;
  border-radius: 6px;
  color: #527399;
}
.turn-content {
  padding: 20px;
  background: #fafbfd;
  min-width: 0;
}
.evaluation-section {
  margin-bottom: 24px;
}
.evaluation-section > h3 {
  margin: 0 0 16px;
  font-size: 16px;
  color: #34435a;
}
.basic-messages {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.basic-messages > .message-card {
  min-width: 0;
}
.message-card {
  background: #fff;
  border: 1px solid #e1e7f1;
  border-top: 3px solid #89acff;
  border-radius: 10px;
  margin-bottom: 16px;
  padding: 16px;
}
.message-card header {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  margin-bottom: 16px;
}
.message-card header span {
  font-size: 11px;
  color: #9da7b7;
}
.expected {
  border-top-color: #7ed9b8;
}
.tools {
  border-top-color: #b294ff;
}
.message-card pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  font-size: 13px;
}
.advanced,
.case-initialization {
  font-size: 12px;
  color: #70829a;
}
.editor-footer {
  display: flex;
  justify-content: space-between;
  padding: 16px;
  border-top: 1px solid #e1e7f1;
  font-size: 12px;
  color: #8090a7;
}
.error {
  color: #c33;
  padding: 0 16px;
}
.remove-round {
  background: transparent;
  border: 0;
  color: #c55;
  font-size: 12px;
}
.empty {
  padding: 60px;
  text-align: center;
  color: #93a0b3;
}
@media (max-width: 760px) {
  .basic-messages {
    grid-template-columns: 1fr;
  }
  .conversation-layout {
    grid-template-columns: 1fr;
  }
  .sample-meta {
    flex-wrap: wrap;
  }
  .sample-meta label {
    min-width: 180px;
  }
  aside {
    border-right: 0;
    max-height: 220px;
    overflow: auto;
  }
}
</style>
