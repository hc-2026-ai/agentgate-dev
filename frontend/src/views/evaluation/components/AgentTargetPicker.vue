<script lang="ts">
import type { TargetDescriptor } from '../../../api/evaluations';
export interface LocalTarget {
  adapter_type: 'local_bank' | 'demo_loan';
  mode: 'base' | 'workflow' | 'cloudshrimp' | null;
  descriptor: TargetDescriptor;
}
export type AgentTypeGroup = 'base/workflow' | 'abcclaw';
export interface PlatformTeam {
  teamId: string;
  teamName: string;
}
export interface PlatformAgent {
  local?: boolean;
  unavailable?: boolean;
  agentId: string;
  agentName: string;
  typeGroup: AgentTypeGroup | null;
  platformAgentType: string | null;
  platformArrangeType: string | null;
  tools?: { function?: { name?: string; description?: string } }[];
  skills?: { id: string; name?: string; description?: string; tools?: string[] }[];
}
export interface PlatformBranch {
  branchId: string;
  branchName: string | null;
  children: readonly PlatformBranch[];
}
export interface PlatformVersion {
  localTarget?: LocalTarget;
  agentVersion: string;
  status?: string;
}
export interface PlatformCapability {
  tools: { name: string; description?: string | null }[];
  skills: {
    external_skill_id: string;
    name: string;
    description?: string | null;
    tools: string[];
  }[];
}
export interface PlatformInitializationVariable {
  name: string;
  type: string;
  required: boolean;
}
export interface AgentTargetSelection {
  localTarget?: LocalTarget;
  teamId: string;
  teamName: string;
  agentId: string;
  agentName: string;
  typeGroup: AgentTypeGroup;
  platformAgentType: string | null;
  platformArrangeType: string | null;
  branchId: string | null;
  branchName: string | null;
  agentVersion: string;
  tools?: PlatformAgent['tools'];
  skills?: PlatformAgent['skills'];
}
// Providers return complete, normalized lists; HTTP and pagination stay outside the UI.
export interface AgentDirectory {
  getTeams(input: { token: string }): Promise<readonly PlatformTeam[]>;
  getAgents(input: { token: string; teamId: string }): Promise<readonly PlatformAgent[]>;
  getBranches(input: { token: string; agentId: string }): Promise<readonly PlatformBranch[]>;
  getAgentVersions(input: { token: string; agentId: string }): Promise<readonly PlatformVersion[]>;
  getBranchVersions(input: {
    token: string;
    agentId: string;
    branchId: string;
  }): Promise<readonly PlatformVersion[]>;
  getCapabilities?(input: {
    token: string;
    agentId: string;
    agentVersion: string;
    branchId?: string | null;
  }): Promise<PlatformCapability>;
  getInitializationVariables?(input: {
    token: string;
    agentId: string;
    agentVersion: string;
  }): Promise<readonly PlatformInitializationVariable[]>;
}
</script>

<script setup lang="ts">
import { computed, getCurrentInstance, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { ElOption, ElSelect } from 'element-plus';

const props = withDefaults(
  defineProps<{
    directory: AgentDirectory;
    token: string;
    teamId: string;
    disabled?: boolean;
    versionLabel?: string;
  }>(),
  {
    disabled: false,
    versionLabel: '智能体版本',
  },
);
const emit = defineEmits<{ 'selection-change': [selection: AgentTargetSelection | null] }>();
type ListKey = 'agent' | 'branch' | 'version';
type LoadState = 'idle' | 'loading' | 'ready' | 'empty' | 'error';
const prefix = `agent-target-${getCurrentInstance()!.uid}`;
const selected = reactive({ agent: '', branch: '', version: '' });
const agents = ref<readonly PlatformAgent[]>([]);
const branches = ref<readonly (PlatformBranch & { depth: number })[]>([]);
const versions = ref<readonly PlatformVersion[]>([]);
const states = reactive<Record<ListKey, LoadState>>({
  agent: 'idle',
  branch: 'idle',
  version: 'idle',
});
const errors = reactive<Record<ListKey, string>>({ agent: '', branch: '', version: '' });
const sequence: Record<ListKey, number> = { agent: 0, branch: 0, version: 0 };
let session = 0;
let disposed = false;
const currentAgent = computed(() => agents.value.find((item) => item.agentId === selected.agent));
const currentBranch = computed(() =>
  branches.value.find((item) => item.branchId === selected.branch),
);
const currentVersion = computed(() =>
  versions.value.find((item) => item.agentVersion === selected.version),
);
const selectedType = computed<AgentTypeGroup | null>(() => currentAgent.value?.typeGroup ?? null);
const requiresBranch = computed(
  () => selectedType.value === 'abcclaw' && !currentAgent.value?.local,
);
const typeError = computed(() => {
  if (!currentAgent.value) return '';
  if (currentAgent.value.unavailable) return '智能体服务未启动，请启动后重试。';
  return selectedType.value ? '' : '该智能体类型未知或存在冲突，暂不支持选择。';
});
const agentAvailable = computed(
  () =>
    states.agent === 'ready' && !!currentAgent.value && !!selectedType.value && !typeError.value,
);
const branchAvailable = computed(
  () => agentAvailable.value && states.branch === 'ready' && !!currentBranch.value,
);
const selection = computed<AgentTargetSelection | null>(() => {
  if (disposed || !agentAvailable.value || states.version !== 'ready' || !currentVersion.value)
    return null;
  if (requiresBranch.value && !branchAvailable.value) return null;
  const agent = currentAgent.value!;
  return {
    teamId: props.teamId,
    teamName: '',
    agentId: agent.agentId,
    agentName: agent.agentName,
    typeGroup: selectedType.value!,
    platformAgentType: agent.platformAgentType,
    platformArrangeType: agent.platformArrangeType,
    branchId: requiresBranch.value ? currentBranch.value!.branchId : null,
    branchName: requiresBranch.value ? currentBranch.value!.branchName : null,
    agentVersion: currentVersion.value.agentVersion,
    ...(currentVersion.value.localTarget ? { localTarget: currentVersion.value.localTarget } : {}),
    tools: agent.tools,
    skills: agent.skills,
  };
});
watch(selection, (value) => emit('selection-change', value ? { ...value } : null), {
  immediate: true,
  flush: 'sync',
});

function clearList(key: ListKey) {
  sequence[key]++;
  states[key] = 'idle';
  errors[key] = '';
  selected[key] = '';
  if (key === 'agent') agents.value = [];
  if (key === 'branch') branches.value = [];
  if (key === 'version') versions.value = [];
}
function clearAfter(key: ListKey) {
  if (key === 'agent') clearList('branch');
  if (key !== 'version') clearList('version');
}
function selectField(key: ListKey, value: string) {
  if (props.disabled || fields.value.find((field) => field.key === key)?.disabled) return;
  if (selected[key] === value) return;
  clearAfter(key);
  selected[key] = value;
}
function context(key: ListKey) {
  return JSON.stringify([
    session,
    props.token,
    props.teamId,
    key === 'branch' || key === 'version' ? selected.agent : '',
    key === 'version' ? selected.branch : '',
  ]);
}
async function loadList<T>(
  key: ListKey,
  fetch: () => Promise<readonly T[]>,
  accept: (items: readonly T[]) => void,
) {
  if (props.disabled || states[key] === 'loading') return;
  const ticket = ++sequence[key];
  const start = context(key);
  states[key] = 'loading';
  errors[key] = '';
  const active = () => !disposed && ticket === sequence[key] && start === context(key);
  try {
    const items = await fetch();
    if (!active()) return;
    accept(items);
    states[key] = items.length ? 'ready' : 'empty';
  } catch (error) {
    if (!active()) return;
    const status = (error as { status?: unknown } | null)?.status;
    errors[key] =
      status === 401 || status === 403
        ? 'Token 无效或无权访问，请重新登录。'
        : '查询失败，请重试。';
    states[key] = 'error';
  }
}
function loadAgents() {
  if (!props.token) return;
  return loadList(
    'agent',
    () => props.directory.getAgents({ token: props.token, teamId: props.teamId }),
    (items) => {
      agents.value = items;
      if (selected.agent && !currentAgent.value) {
        selected.agent = '';
        clearAfter('agent');
      }
    },
  );
}
function flattenBranches(
  items: readonly PlatformBranch[],
  depth = 0,
): (PlatformBranch & { depth: number })[] {
  return items.flatMap((item) => [
    { ...item, depth },
    ...flattenBranches(item.children, depth + 1),
  ]);
}
function loadBranches() {
  if (!agentAvailable.value || !requiresBranch.value) return;
  return loadList(
    'branch',
    () => props.directory.getBranches({ token: props.token, agentId: selected.agent }),
    (items) => {
      branches.value = flattenBranches(items);
      if (selected.branch && !currentBranch.value) {
        selected.branch = '';
        clearAfter('branch');
      }
    },
  );
}
function loadVersions() {
  if (!agentAvailable.value || (requiresBranch.value && !branchAvailable.value)) return;
  return loadList(
    'version',
    () => {
      const input = { token: props.token, agentId: selected.agent };
      return requiresBranch.value
        ? props.directory.getBranchVersions({ ...input, branchId: selected.branch })
        : props.directory.getAgentVersions(input);
    },
    (items) => {
      versions.value = items;
      if (!currentVersion.value) selected.version = '';
    },
  );
}
const loaders = { agent: loadAgents, branch: loadBranches, version: loadVersions };
const fields = computed(() => [
  {
    key: 'agent' as const,
    label: '选择智能体',
    placeholder: '请选择智能体',
    disabled: !props.token,
    options: agents.value.map((item) => ({
      value: item.agentId,
      label: `${item.agentName}${item.unavailable ? '（服务离线）' : ''}`,
    })),
  },
  {
    key: 'branch' as const,
    label: '分支地址',
    placeholder: !requiresBranch.value ? '不适用（无平台分支）' : '请选择分支地址',
    disabled: !agentAvailable.value || !requiresBranch.value,
    options: branches.value.map((item) => ({
      value: item.branchId,
      label: `${'↳ '.repeat(item.depth)}${item.branchName || item.branchId} · ${item.branchId}`,
    })),
  },
  {
    key: 'version' as const,
    label: props.versionLabel,
    placeholder: requiresBranch.value && !selected.branch ? '请先选择分支地址' : '请选择版本',
    disabled: !agentAvailable.value || (requiresBranch.value && !branchAvailable.value),
    options: versions.value.map((item) => ({
      value: item.agentVersion,
      label: item.status ? `${item.agentVersion} · ${item.status}` : item.agentVersion,
    })),
  },
]);
function openField(key: ListKey, open: boolean) {
  if (open) void loaders[key]();
}
function readSubmissionSelection(): { target: AgentTargetSelection; token: string } | null {
  return !disposed && selection.value
    ? { target: { ...selection.value }, token: props.token }
    : null;
}
defineExpose({ readSubmissionSelection });
function reset() {
  session++;
  clearList('agent');
  clearAfter('agent');
}
watch(() => props.directory, reset);
watch([() => props.token, () => props.teamId], reset);
onBeforeUnmount(() => {
  disposed = true;
  emit('selection-change', null);
});
</script>

<template>
  <section class="agent-target-picker" aria-label="测评对象选择">
    <div class="target-fields">
      <div v-for="field in fields" :key="field.key" class="target-field">
        <label :id="`${prefix}-${field.key}-label`" :for="`${prefix}-${field.key}`">{{
          field.label
        }}</label>
        <ElSelect
          :id="`${prefix}-${field.key}`"
          :model-value="selected[field.key]"
          :aria-label="field.label"
          :role="disabled || field.disabled ? 'combobox' : undefined"
          :aria-labelledby="disabled || field.disabled ? `${prefix}-${field.key}-label` : undefined"
          :aria-disabled="disabled || field.disabled ? true : undefined"
          :aria-expanded="disabled || field.disabled ? false : undefined"
          :placeholder="field.placeholder"
          :disabled="disabled || field.disabled"
          :loading="states[field.key] === 'loading'"
          loading-text="正在加载…"
          no-data-text="暂无可选项"
          :teleported="false"
          @visible-change="(open: boolean) => openField(field.key, open)"
          @change="(value: string) => selectField(field.key, value)"
        >
          <ElOption
            v-for="option in field.options"
            :key="option.value"
            :label="option.label"
            :value="option.value"
            :disabled="states[field.key] !== 'ready'"
          />
        </ElSelect>
        <p v-if="states[field.key] === 'loading'" role="status">
          正在加载{{ field.label.replace('选择', '') }}…
        </p>
        <p v-if="states[field.key] === 'empty'" role="status">
          暂无可选{{ field.label.replace('选择', '') }}。
        </p>
        <div v-if="states[field.key] === 'error'" class="field-error">
          <span role="alert">{{ errors[field.key] }}</span>
          <button
            type="button"
            :aria-label="`重试${field.label}`"
            :disabled="disabled || field.disabled"
            @click="loaders[field.key]()"
          >
            重试
          </button>
        </div>
        <p v-if="field.key === 'agent' && typeError" role="alert" class="field-error">
          {{ typeError }}
        </p>
      </div>
    </div>
    <p class="target-status" role="status">
      {{
        selection
          ? `已选择：${selection.agentName} / ${selection.typeGroup}${selection.branchId ? ' / ' + selection.branchId : ''} / ${selection.agentVersion}`
          : '请完成测评对象选择。'
      }}
    </p>
  </section>
</template>

<style scoped>
.agent-target-picker {
  color: var(--el-text-color-primary);
}
.agent-target-picker * {
  box-sizing: border-box;
}
.target-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
button {
  padding: 9px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-bg-color);
  color: var(--el-color-primary);
  font: inherit;
  white-space: nowrap;
  cursor: pointer;
}
button:disabled {
  color: var(--el-text-color-disabled);
  cursor: not-allowed;
}
.target-fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 20px;
}
.target-field :deep(.el-select) {
  width: 100%;
}
.target-field :deep(.el-select-dropdown__item) {
  max-width: 100%;
}
.target-field p,
.field-error {
  margin: 0;
  overflow-wrap: anywhere;
}
.field-error {
  color: var(--el-color-danger);
}
.field-error button {
  margin-left: 8px;
}
.target-status {
  padding: 12px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  overflow-wrap: anywhere;
  margin: 16px 0 0;
}
@media (max-width: 900px) {
  .target-fields {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
