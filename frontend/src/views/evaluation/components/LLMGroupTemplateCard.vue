<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { request, type EvaluatorDetail, type EvaluatorSummary } from '../../../api/evaluations';
import {
  llmEvaluatorCardContent,
  llmGroupTemplates,
  type LLMGroupDetail,
} from '../utils/evaluator-group';
import { evaluatorTemplates } from '../utils/evaluator-templates';

const props = defineProps<{
  template?: (typeof llmGroupTemplates)[number];
  evaluator?: EvaluatorSummary;
  disabled?: boolean;
  action?: 'apply' | 'view';
}>();
const emit = defineEmits<{ apply: [id: string]; view: [id: string] }>();
const detail = ref<EvaluatorDetail | LLMGroupDetail | null>(null);
const loading = ref(false),
  error = ref('');
let sequence = 0;
const card = computed(() => {
  if (props.template) {
    return {
      name: props.template.name,
      description: props.template.description,
      itemLabel: '子评估器',
      children: props.template.children.map((child) => ({
        id: child.templateId,
        name: evaluatorTemplates.find((item) => item.id === child.templateId)?.name ?? '',
        weight: child.weight,
      })),
    };
  }
  return detail.value
    ? llmEvaluatorCardContent(detail.value)
    : {
        name: props.evaluator?.name ?? '',
        description: props.evaluator?.description ?? '',
        itemLabel: '子评估器',
        children: [],
      };
});
async function load() {
  const ticket = ++sequence;
  detail.value = null;
  error.value = '';
  if (!props.evaluator) return;
  loading.value = true;
  try {
    const value = await request<EvaluatorDetail | LLMGroupDetail>(
      '/evaluators/' +
        encodeURIComponent(props.evaluator.id) +
        (props.evaluator.kind === 'llm_group' ? '/llm-group' : ''),
    );
    llmEvaluatorCardContent(value);
    if (ticket === sequence) detail.value = value;
  } catch {
    if (ticket === sequence) error.value = '读取评估配置失败，请重试。';
  } finally {
    if (ticket === sequence) loading.value = false;
  }
}
watch(() => props.evaluator, load, { immediate: true });
onUnmounted(() => sequence++);
function activate() {
  const id = props.template?.id ?? props.evaluator?.id;
  if (!id) return;
  if (props.action === 'view') emit('view', id);
  else emit('apply', id);
}
</script>

<template>
  <article class="llm-group-template-card">
    <div class="template-card-header">
      <span class="asset-chip">{{
        evaluator ? (action === 'view' ? '自建' : '自建模板') : '预置模板'
      }}</span>
      <slot name="menu" />
    </div>
    <h2 class="group-template-title">{{ card.name }}</h2>
    <p>{{ card.description || '暂无备注描述' }}</p>
    <div class="evaluator-metadata">
      <div class="group-template-tags"><span>LLM</span><span>文本</span><span>内容质量</span></div>
      <div v-if="evaluator && action === 'view'" class="asset-chips evaluator-version-status">
        <span v-if="evaluator.latest_version" class="asset-chip" title="最新已发布版本">
          V{{ evaluator.latest_version }}
        </span>
        <span v-if="evaluator.has_draft" class="asset-chip preview">未发布草稿</span>
      </div>
    </div>
    <p v-if="loading" role="status">正在读取评估配置…</p>
    <p v-else-if="error" role="alert">
      {{ error }} <button class="asset-link" @click="load">重试</button>
    </p>
    <template v-else>
      <h4>包含 {{ card.children.length }} 个{{ card.itemLabel }}</h4>
      <ul class="group-template-children">
        <li v-for="child in card.children" :key="child.id">
          <span>{{ child.name }}</span>
          <strong>{{ child.weight === null ? '—' : child.weight + '%' }}</strong>
        </li>
      </ul>
    </template>
    <div class="asset-card-actions">
      <button
        :class="action === 'view' ? 'asset-link' : 'llm-template-create-button'"
        :disabled="disabled || loading || !!error"
        :aria-label="action === 'view' && template ? '查看' + card.name + '模板' : undefined"
        @click="activate"
      >
        {{ action === 'view' ? '查看详情' : '使用此模版创建' }}
      </button>
    </div>
  </article>
</template>

<style scoped>
.llm-group-template-card {
  align-self: stretch;
  font-size: 14px;
  line-height: 1.6;
}
.template-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.template-card-header .asset-chip {
  flex: 1;
  min-width: 0;
}
.llm-group-template-card .asset-card-actions {
  min-height: 54px;
}
.evaluator-metadata {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.group-template-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.group-template-tags span {
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color);
  border-radius: 3px;
  padding: 3px 6px;
  font-size: 10px;
}
.llm-group-template-card h4 {
  margin: 0;
  color: var(--el-text-color-regular);
  font-size: 12px;
  font-weight: 500;
}
.group-template-children {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.group-template-children li {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  border-radius: 5px;
  padding: 8px;
  background: var(--el-fill-color-extra-light);
  color: var(--el-text-color-regular);
  font-size: 12px;
}
.group-template-children li span {
  overflow-wrap: anywhere;
  min-width: 0;
}
.group-template-children strong {
  color: var(--el-color-primary-dark-2);
  font-weight: 500;
  flex-shrink: 0;
}
.asset-link:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 3px;
}
.asset-link:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
