<script setup lang="ts">
import { useRouter } from 'vue-router';
import { readTaskLinks } from '../utils/task-links';
const router = useRouter();
import { shallowRef, computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
  api,
  request,
  statusLabel,
  score,
  type RunSamples,
  type RunProgress,
  type Report,
  type Trace,
} from '../../../api/evaluations';
import { ElMessageBox } from 'element-plus';
import {
  scoreColumns,
  columnScore,
  sampleSummary,
  downloadCsv,
  displayValue,
  traceSeconds,
  groupEvaluatorResults,
} from '../utils/task-report';
import ResultEvaluationCard from './ResultEvaluationCard.vue';
import RerunDialog from './RerunDialog.vue';
import TraceExplorer from './TraceExplorer.vue';
import { sameJson } from '../utils/report-presentation';
import { taskTitle } from '../utils/dashboard-data';
const detailTabs = [
  ['results', '执行结果'],
  ['low-samples', '低分样本'],
  ['iteration', '优化迭代'],
  ['config', '任务快照'],
] as const;
type DetailTab = (typeof detailTabs)[number][0];
const props = defineProps<{ id: string; taskId?: string; returnPage?: string }>(),
  emit = defineEmits<{ navigate: [path: string]; 'rerun-created': [taskId: string] }>();
const writing = ref(false);
async function writeback() {
  if (!sample.value || writing.value) return;
  try {
    await ElMessageBox.confirm(
      '将本次样本快照写回原测评集草稿？同 ID 草稿样本将被替换，已发布版本和历史结果不变。',
      '回写失败样本',
      { confirmButtonText: '确认回写', cancelButtonText: '取消' },
    );
    writing.value = true;
    const r = await request<{ source_dataset_id: string }>(
      `/runs/${props.id}/cases/${encodeURIComponent(sample.value.id)}/writeback`,
      'POST',
      { case: sample.value },
    );
    emit('navigate', 'datasets/' + r.source_dataset_id + '?version=draft');
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') error.value = String(e);
  } finally {
    writing.value = false;
  }
}
const progress = ref<RunProgress | null>(null),
  data = shallowRef<RunSamples | null>(null),
  report = shallowRef<Report | null>(null),
  error = ref(''),
  tab = ref<DetailTab>(
    detailTabs.find(([key]) => key === router.currentRoute.value.query.tab)?.[0] ?? 'results',
  ),
  rerun = ref(false);
const sampleId = computed(() =>
    decodeURIComponent(router.currentRoute.value.path.match(/\/samples\/([^/]+)/)?.[1] ?? ''),
  ),
  turnIndex = ref(0),
  continuous = ref(false),
  status = ref(''),
  range = ref(''),
  scenario = ref('');
const traces = ref<Record<string, Trace>>({}),
  traceErrors = ref<Record<string, string>>({}),
  exporting = ref(false),
  exportError = ref('');
const traceRequests = new Map<string, Promise<boolean>>();
let timer: ReturnType<typeof setTimeout> | undefined,
  disposed = false,
  loadTicket = 0;
const manifest = computed<any>(() => data.value?.run.manifest);
const associatedTask = computed(() => readTaskLinks().find((t) => t.runIds.includes(props.id)));
const canRerun = computed(
  () =>
    associatedTask.value?.kind === 'single' &&
    ['completed', 'failed', 'cancelled'].includes(progress.value?.status ?? ''),
);
const cases = computed(
  () =>
    manifest.value?.dataset.cases.filter(
      (c: any) =>
        !manifest.value.selected_case_ids || manifest.value.selected_case_ids.includes(c.id),
    ) ?? [],
);
const specs = computed<any[]>(
  () =>
    manifest.value?.evaluator_specs.filter((s: any) =>
      manifest.value.primary_evaluator_ids.includes(s.id),
    ) ?? [],
);
const columns = computed(() => (manifest.value ? scoreColumns(manifest.value) : []));
const caseResults = (id: string) => data.value?.results.filter((r) => r.case_id === id) ?? [];
const summary = (id: string) => {
  const result = sampleSummary(caseResults(id), manifest.value?.primary_evaluator_ids ?? []);
  return result.outcome === 'pending' && progress.value?.status === 'failed'
    ? { ...result, outcome: 'error' }
    : result;
};
const scenarios = computed<string[]>(() => [
  ...new Set<string>(cases.value.flatMap((c: any) => c.tags ?? [])),
]);
const filtered = computed(() =>
  cases.value.filter((c: any) => {
    const s = summary(c.id);
    return (
      (!status.value || s.outcome === status.value) &&
      (!scenario.value || c.tags.includes(scenario.value)) &&
      (tab.value === 'low-samples'
        ? s.score !== null && s.score < 0.8
        : !range.value ||
          (s.score !== null && (range.value === 'high' ? s.score >= 0.8 : s.score < 0.8)))
    );
  }),
);
const sample = computed(() => cases.value.find((c: any) => c.id === sampleId.value)),
  trace = computed(() => traces.value[sampleId.value]),
  results = computed(() => caseResults(sampleId.value));
const isMultiTurn = computed(() => (sample.value?.turns.length ?? 0) > 1);
const groupedResults = computed(() =>
  groupEvaluatorResults(results.value, manifest.value?.evaluator_specs).sort(
    (a, b) =>
      Number(b.result.evaluator_kind === 'rule') - Number(a.result.evaluator_kind === 'rule'),
  ),
);
function childSnapshots(parent: any) {
  return (parent.children ?? []).flatMap((ref: any) => {
    const child = manifest.value?.evaluator_specs.find(
      (s: any) => s.id === ref.evaluator_id && s.version === ref.evaluator_version,
    );
    return child ? [{ ...child, weight: ref.weight }] : [];
  });
}
const shownTurns = computed(() =>
  !isMultiTurn.value || continuous.value
    ? (sample.value?.turns ?? [])
    : (sample.value?.turns.slice(turnIndex.value, turnIndex.value + 1) ?? []),
);
const stats = computed(() => {
  const summaries = cases.value.map((c: any) => summary(c.id));
  return {
    pass: summaries.filter((s: any) => s.outcome === 'pass').length,
  };
});
const overall = computed(
  () => report.value?.metrics.find((m) => m.level === 'overall')?.score ?? null,
);
const navigableCases = computed(() => (tab.value === 'low-samples' ? filtered.value : cases.value));
const currentPosition = computed(() =>
  navigableCases.value.findIndex((c: any) => c.id === sampleId.value),
);
const formatTime = (n: number | null) =>
  n === null ? '—' : n < 0.01 ? '< 0.01s' : n.toFixed(2) + 's';
function openSample(id: string) {
  turnIndex.value = 0;
  void router.replace({
    path: `/tasks/${props.taskId ?? associatedTask.value?.id ?? props.id}/samples/${encodeURIComponent(id)}`,
    query: {
      ...(tab.value === 'low-samples' ? { tab: 'low-samples' } : {}),
      ...(associatedTask.value?.kind === 'ab' ? { run: props.id } : {}),
    },
  });
}
function back() {
  void router.replace({
    path: `/tasks/${props.taskId ?? associatedTask.value?.id ?? props.id}`,
    query: tab.value === 'low-samples' ? { tab: 'low-samples' } : {},
  });
}
function move(n: number) {
  const c = navigableCases.value[currentPosition.value + n];
  if (c) openSample(c.id);
}
function toolsFor(id: string) {
  const spans = trace.value?.spans ?? [],
    ids = new Set(
      spans.filter((s) => s.attributes['agentgate.turn.id'] === id).map((s) => s.span_id),
    );
  let changed = true;
  while (changed) {
    changed = false;
    for (const s of spans as any[])
      if (ids.has(s.parent_span_id) && !ids.has(s.span_id)) {
        ids.add(s.span_id);
        changed = true;
      }
  }
  return spans.filter((s) => s.operation_type === 'tool' && ids.has(s.span_id));
}
function loadTrace(id: string): Promise<boolean> {
  if (disposed) return Promise.resolve(false);
  if (traces.value[id]) return Promise.resolve(true);
  const pending = traceRequests.get(id);
  if (pending) return pending;
  delete traceErrors.value[id];
  const request = (async () => {
    try {
      const value = await api.trace(props.id, id);
      if (disposed) return false;
      traces.value[id] = value;
      return true;
    } catch (e) {
      if (!disposed) traceErrors.value[id] = String(e);
      return false;
    } finally {
      traceRequests.delete(id);
    }
  })();
  traceRequests.set(id, request);
  return request;
}
function loadSelectedTrace() {
  if (sample.value && caseResults(sample.value.id).length) void loadTrace(sample.value.id);
}
async function load() {
  clearTimeout(timer);
  if (disposed || document.hidden) return;
  const ticket = ++loadTicket;
  try {
    const p = await api.status(props.id);
    if (disposed || ticket !== loadTicket) return;
    const r = p.status === 'completed' ? await api.report(props.id) : null,
      s = r ? { run: r.run, results: r.results, complete: true } : await api.samples(props.id);
    if (disposed || ticket !== loadTicket) return;
    progress.value = p;
    report.value = r;
    data.value = s;
    error.value = '';
    loadSelectedTrace();
  } catch (e) {
    if (!disposed && ticket === loadTicket) error.value = String(e);
  } finally {
    if (
      !disposed &&
      ticket === loadTicket &&
      !document.hidden &&
      (error.value || !['completed', 'failed', 'cancelled'].includes(progress.value?.status ?? ''))
    )
      timer = setTimeout(load, 5000);
  }
}
function visibilityChanged() {
  loadTicket++;
  clearTimeout(timer);
  if (!document.hidden) void load();
}
async function exportRows() {
  if (exporting.value) return;
  exporting.value = true;
  exportError.value = '';
  const rows = [...filtered.value];
  const ids = rows.filter((c: any) => caseResults(c.id).length).map((c: any) => c.id);
  try {
    for (let start = 0; start < ids.length; start += 4) {
      if (disposed) return;
      const loaded = await Promise.all(ids.slice(start, start + 4).map(loadTrace));
      if (loaded.some((success) => !success)) {
        exportError.value = '部分样本执行轨迹读取失败，未导出；请重试，以免遗漏耗时数据。';
        return;
      }
    }
    if (disposed) return;
    downloadCsv(
      [
        ['用户输入', '场景', '状态', '综合分', ...columns.value.map((c) => c.name), '耗时（秒）'],
        ...rows.map((c: any) => [
          displayValue(c.turns[0]?.input),
          c.tags.join('、'),
          statusLabel(summary(c.id).outcome),
          score(summary(c.id).score),
          ...columns.value.map((col) => score(columnScore(caseResults(c.id), col))),
          traceSeconds(traces.value[c.id]),
        ]),
      ],
      `测评结果-${props.id}.csv`,
    );
  } finally {
    exporting.value = false;
  }
}
onMounted(() => {
  document.addEventListener('visibilitychange', visibilityChanged);
  void load();
});
onUnmounted(() => {
  disposed = true;
  loadTicket++;
  clearTimeout(timer);
  document.removeEventListener('visibilitychange', visibilityChanged);
});
watch(
  () => router.currentRoute.value.query.tab,
  (value) => (tab.value = detailTabs.find(([key]) => key === value)?.[0] ?? 'results'),
);
watch(sampleId, () => {
  turnIndex.value = 0;
  continuous.value = false;
  loadSelectedTrace();
});
</script>
<template>
  <div class="task-results">
    <div class="breadcrumbs">
      <button class="link" @click="emit('navigate', 'tasks')">测评任务</button><span>/</span
      ><button class="link" @click="back">任务详情</button
      ><template v-if="sampleId"><span>/</span><span>样本详情</span></template>
    </div>
    <p v-if="error" class="notice error" role="alert">
      {{ error }} <button class="link" @click="load">重试</button>
    </p>
    <template v-if="data && progress">
      <template v-if="!sampleId">
        <header class="task-hero">
          <div class="hero-icon">{{ progress.status === 'completed' ? '✓' : '◷' }}</div>
          <div>
            <h1>
              {{
                taskTitle(
                  data.run,
                  undefined,
                  readTaskLinks().find((t) => t.runIds.includes(data?.run.id ?? ''))?.name,
                )
              }}
            </h1>
            <p>
              {{ manifest.target.ref.external_version_id }} <span>·</span>
              {{ new Date(progress.created_at).toLocaleString() }}
            </p>
          </div>
          <span
            class="badge"
            :class="
              progress.status === 'failed'
                ? 'error'
                : progress.status === 'completed'
                  ? 'success'
                  : 'info'
            "
            >{{ statusLabel(progress.status) }}</span
          >
        </header>
        <nav class="tabs" aria-label="测评任务详情页签">
          <button
            v-for="t in detailTabs"
            :key="t[0]"
            class="tab"
            :class="{ active: tab === t[0] }"
            :aria-pressed="tab === t[0]"
            @click="tab = t[0]"
          >
            {{ t[1] }}
          </button>
        </nav>
        <p v-if="progress.error" class="notice error">{{ progress.error }}</p>
        <template v-if="tab === 'results' || tab === 'low-samples'">
          <section v-if="tab === 'results'" class="task-kpis">
            <article>
              <span>完成进度</span><b>{{ Math.round(progress.progress * 100) }}%</b>
            </article>
            <article>
              <span>已处理 / 总样本</span
              ><b
                >{{ progress.completed_cases }} <small>/ {{ progress.total_cases }}</small></b
              >
            </article>
            <article>
              <span>当前得分</span><b class="violet">{{ score(overall) }}</b>
            </article>
            <article>
              <span>样本通过率</span
              ><b class="green"
                >{{ cases.length ? Math.round((stats.pass / cases.length) * 100) : 0 }}%</b
              >
            </article>
          </section>
          <p
            v-if="tab === 'results' && report && report.release_gate.outcome !== 'pass'"
            class="notice warn"
          >
            任务判定：{{ statusLabel(report.release_gate.outcome) }} ·
            {{
              report.release_gate.reason_code === 'evaluator_error'
                ? '评估器执行异常'
                : report.release_gate.reason_code === 'no_applicable_results'
                  ? '无适用检查'
                  : report.release_gate.reason_code
            }}
          </p>
          <p v-if="tab === 'low-samples'" class="muted">
            展示综合分低于 80 分的样本，未产生分数的样本不计入。
          </p>
          <div class="toolbar result-filters">
            <select class="input" v-model="status" aria-label="样本状态">
              <option value="">全部状态</option>
              <option
                v-for="s in ['pass', 'fail', 'error', 'review', 'pending', 'not_applicable']"
                :key="s"
                :value="s"
              >
                {{ statusLabel(s) }}
              </option></select
            ><select v-if="tab === 'results'" class="input" v-model="range" aria-label="分数区间">
              <option value="">全部分数</option>
              <option value="high">80—100 分</option>
              <option value="low">低于 80 分</option></select
            ><select class="input" v-model="scenario" aria-label="样本场景">
              <option value="">全部场景</option>
              <option v-for="(s, rowIndex1) in scenarios" :key="rowIndex1">{{ s }}</option></select
            ><button class="secondary export" :disabled="exporting" @click="exportRows">
              {{ exporting ? '正在准备导出…' : '导出 CSV' }}
            </button>
          </div>
          <p v-if="exportError" class="notice error" role="alert">{{ exportError }}</p>
          <div
            class="table-wrap sample-results-scroll"
            role="region"
            aria-label="执行结果表格"
            tabindex="0"
          >
            <table
              class="data-table sample-table"
              :style="{ '--score-column-count': columns.length }"
            >
              <colgroup>
                <col class="sample-input-column" />
                <col class="sample-scene-column" />
                <col class="sample-status-column" />
                <col class="sample-score-column" />
                <col v-for="c in columns" :key="c.key" />
                <col class="sample-action-column" />
              </colgroup>
              <thead>
                <tr>
                  <th scope="col">用户输入</th>
                  <th scope="col">场景</th>
                  <th scope="col">状态</th>
                  <th scope="col">综合分</th>
                  <th v-for="c in columns" :key="c.key" scope="col" :title="c.name">
                    {{ c.name }}
                  </th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in filtered" :key="c.id">
                  <td>
                    <b class="sample-name" :title="c.name">{{ c.name }}</b>
                    <p class="query-preview" :title="displayValue(c.turns[0]?.input)">
                      {{ displayValue(c.turns[0]?.input) }}
                    </p>
                  </td>
                  <td>
                    <span class="sample-scenario" :title="c.tags.join('、')">
                      {{ c.tags.join('、') || '—' }}
                    </span>
                  </td>
                  <td>{{ statusLabel(summary(c.id).outcome) }}</td>
                  <td class="green">{{ score(summary(c.id).score) }}</td>
                  <td v-for="col in columns" :key="col.key" class="violet">
                    {{ score(columnScore(caseResults(c.id), col)) }}
                  </td>
                  <td><button class="link" @click="openSample(c.id)">样本详情</button></td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="!filtered.length" class="empty">
            {{ tab === 'low-samples' ? '暂无匹配的低分样本' : '暂无匹配样本' }}
          </p>
        </template>
        <section v-else-if="tab === 'iteration'" aria-label="优化迭代">
          <p class="empty">能力构建中</p>
        </section>
        <section v-else-if="tab === 'config'" class="config-cards">
          <article class="card">
            <h2>被测智能体</h2>
            <div class="config-block">
              <b>{{ manifest.target.display_name }}</b>
              <p>
                {{ manifest.target.ref.external_target_id }} ·
                {{ manifest.target.ref.external_version_id }}
              </p>
            </div>
            <h3>测评集</h3>
            <div class="config-block">
              <a class="link" :href="'#datasets/' + manifest.dataset.dataset_id">{{
                manifest.dataset.dataset_name
              }}</a>
              <p>{{ cases.length }} 条样本 · v{{ manifest.dataset.version }}</p>
            </div>
            <h3>执行设置</h3>
            <p>
              并发 {{ manifest.max_parallel_cases }} · 超时 {{ manifest.timeout_seconds }} 秒 · 重试
              {{ manifest.max_retries }} 次
            </p>
            <button v-if="canRerun" class="secondary" @click="rerun = true">使用原配置重跑</button>
          </article>
          <article class="card">
            <h2>评估器配置</h2>
            <div v-for="s in specs" :key="s.id" class="config-block">
              <b
                >{{ s.name }}
                <span class="badge info">{{ s.kind === 'rule' ? '规则评估' : 'LLM 评估' }}</span></b
              >
              <p>v{{ s.version }} · {{ s.dimension }} · {{ s.metric }}</p>
              <p v-if="s.config?.model">
                {{ s.config.model.provider_id }} · {{ s.config.model.model_id }}
              </p>
              <p v-if="s.kind === 'llm_judge' || s.kind === 'llm_group'">
                通过分数 {{ Number(s.config.pass_threshold ?? 0.8) * 100 }} / 100
              </p>
              <details v-if="s.kind === 'llm_group'" open>
                <summary>子评估器配置（{{ s.children.length }}）</summary>
                <div v-for="child in childSnapshots(s)" :key="child.id" class="config-block">
                  <b
                    >{{ child.name }} · v{{ child.version }} ·
                    {{ Number((child.weight * 100).toFixed(2)) }}%</b
                  >
                  <p>
                    {{ child.config.model?.provider_id }} · {{ child.config.model?.model_id }} ·
                    通过分数 {{ Number(child.config.pass_threshold ?? 0.8) * 100 }} / 100
                  </p>
                  <p>{{ s.config.child_metadata?.[child.id]?.description }}</p>
                  <details>
                    <summary>Prompt</summary>
                    <pre>{{ child.config.instruction }}</pre>
                    <pre v-if="child.config.user_prompt">{{ child.config.user_prompt }}</pre>
                  </details>
                </div>
              </details>
              <dl
                v-if="s.implementation_id === 'answer_quality' && s.implementation_version === '2'"
              >
                <template v-for="dimension in s.config.dimensions" :key="dimension.id">
                  <dt>{{ dimension.name }} · {{ dimension.weight }}%</dt>
                  <dd>
                    {{ dimension.description }}
                    <pre>{{ dimension.prompt }}</pre>
                  </dd>
                </template>
              </dl>
              <dl v-if="s.config?.rubric">
                <template v-for="(text, key) in s.config.rubric" :key="key"
                  ><dt>{{ key }}</dt>
                  <dd>{{ text }}</dd></template
                >
              </dl>
            </div>
            <p class="muted">
              {{
                manifest.metric_plan.id === 'p1-equal-mean'
                  ? '先计算每条用例的有效评估器均分，再对有得分的用例等权平均'
                  : manifest.metric_plan.id
              }}
            </p>
          </article>
        </section>
      </template>
      <template v-else-if="sample">
        <header class="sample-head">
          <div>
            <h1>样本测评详情</h1>
            <p>{{ sample.name }} · {{ sample.turns.length }} 轮对话</p>
          </div>
          <span class="badge info">{{ statusLabel(summary(sample.id).outcome) }}</span
          ><button class="secondary" :disabled="currentPosition <= 0" @click="move(-1)">
            上一条</button
          ><button
            class="secondary"
            :disabled="currentPosition >= navigableCases.length - 1"
            @click="move(1)"
          >
            下一条
          </button>
        </header>
        <div v-if="results.some((r) => r.outcome === 'fail')" class="toolbar">
          <button class="secondary" :disabled="writing" @click="writeback">
            回写失败样本到草稿
          </button>
        </div>
        <div class="conversation-layout">
          <section class="conversation-panel">
            <header>
              <h2>{{ isMultiTurn ? '多轮对话' : '单轮对话' }}</h2>
              <div v-if="isMultiTurn" class="actions" role="radiogroup" aria-label="对话展示方式">
                <button
                  class="secondary"
                  :class="{ selected: !continuous }"
                  role="radio"
                  :aria-checked="!continuous"
                  @click="continuous = false"
                >
                  单轮聚焦</button
                ><button
                  class="secondary"
                  :class="{ selected: continuous }"
                  role="radio"
                  :aria-checked="continuous"
                  @click="continuous = true"
                >
                  连续对话
                </button>
              </div>
            </header>
            <div class="conversation-body">
              <aside>
                <h4>轮次目录</h4>
                <button
                  v-for="(turn, i) in sample.turns"
                  :key="turn.id"
                  :class="{ active: turnIndex === i }"
                  @click="
                    turnIndex = i;
                    continuous = false;
                  "
                >
                  第 {{ Number(i) + 1 }} 轮 <span>{{ displayValue(turn.input) }}</span>
                </button>
              </aside>
              <main tabindex="0" aria-label="对话内容，可滚动">
                <p v-if="traceErrors[sample.id]" class="notice error">
                  {{ traceErrors[sample.id] }}
                </p>
                <p v-else-if="!trace" class="muted">
                  {{ caseResults(sample.id).length ? '正在读取执行轨迹…' : '尚无执行轨迹' }}
                </p>
                <article v-for="turn in shownTurns" :key="turn.id" class="conversation-turn">
                  <h3>第 {{ sample.turns.indexOf(turn) + 1 }} 轮</h3>
                  <h4>用户输入</h4>
                  <pre class="message user">{{
                    displayValue(trace?.turn_outcomes?.[turn.id]?.input ?? turn.input)
                  }}</pre>
                  <details
                    v-if="
                      trace?.turn_outcomes?.[turn.id] &&
                      !sameJson(turn.input, trace.turn_outcomes[turn.id].input)
                    "
                    class="notice warn"
                  >
                    <summary>实际输入与样本配置不同</summary>
                    <pre>{{ displayValue(turn.input) }}</pre>
                  </details>
                  <h4>期望结果</h4>
                  <div class="message expected">
                    <p v-if="!turn.expectations.length">未设置期望</p>
                    <div v-for="e in turn.expectations" :key="e.id">
                      <b>{{ e.name || e.kind }} {{ e.path || e.tool || e.policy_id || '' }}</b>
                      <pre>{{ displayValue(e.condition ?? e) }}</pre>
                    </div>
                  </div>
                  <h4>实际回答</h4>
                  <pre class="message answer">{{
                    displayValue(trace?.turn_outcomes?.[turn.id]?.output)
                  }}</pre>
                  <details v-if="toolsFor(turn.id).length" class="message">
                    <summary>工具调用（{{ toolsFor(turn.id).length }}）</summary>
                    <div v-for="tool in toolsFor(turn.id)" :key="tool.span_id">
                      <h4>{{ tool.name }}</h4>
                      <pre>{{ displayValue(tool.attributes) }}</pre>
                    </div>
                  </details>
                </article>
              </main>
            </div>
          </section>
          <aside class="evaluation-panel">
            <h2>测评结果</h2>
            <details open>
              <summary>评估结论与依据</summary>
              <ResultEvaluationCard
                v-for="group in groupedResults"
                :key="group.result.evaluator_id"
                :result="group.result"
                :child-results="group.children"
                :primary="manifest.primary_evaluator_ids.includes(group.result.evaluator_id)"
                :turns="sample.turns"
              />
            </details>
          </aside>
        </div>
        <TraceExplorer v-if="trace" :key="trace.trace_id + ':' + sample.id" :trace="trace" />
        <section v-else class="trace-status-panel" aria-label="执行轨迹追踪">
          <h2>执行轨迹追踪</h2>
          <p v-if="traceErrors[sample.id]" class="notice error" role="alert">
            Trace 读取失败：{{ traceErrors[sample.id] }}
            <button class="link" @click="loadSelectedTrace">重试读取轨迹</button>
          </p>
          <p v-else class="muted" role="status">
            {{ caseResults(sample.id).length ? '正在读取完整 Trace…' : '尚无执行轨迹' }}
          </p>
        </section>
      </template>
      <p v-else class="empty">
        样本不存在 <button class="link" @click="back">返回任务详情</button>
      </p>
    </template>
    <p v-else-if="!error" class="empty">正在加载任务…</p>
    <RerunDialog
      v-if="rerun"
      :id="id"
      :task-id="taskId ?? associatedTask?.id"
      @close="rerun = false"
      @created="
        (next) => {
          rerun = false;
          emit('rerun-created', next);
        }
      "
    />
  </div>
</template>
<style scoped>
.tabs {
  overflow-x: auto;
}
.tab {
  flex-shrink: 0;
  white-space: nowrap;
}
.trace-status-panel {
  min-width: 0;
  margin-top: 20px;
  padding: 20px;
  border: 1px solid #dfe8e5;
  border-radius: 14px;
  background: white;
}
.trace-status-panel h2 {
  margin: 0 0 16px;
  font-size: 18px;
}
.breadcrumbs {
  display: flex;
  gap: 12px;
  align-items: center;
  color: #82918b;
  margin-bottom: 20px;
}
.task-hero,
.sample-head {
  display: flex;
  align-items: center;
  gap: 20px;
  margin-bottom: 28px;
}
.task-hero {
  border: 1px solid #e0e8e4;
  padding: 10px 28px;
  border-radius: 14px;
  background: white;
}
.hero-icon {
  padding: 8px 23px;
  background: #ddf5eb;
  color: #00a88b;
  font-size: 32px;
  border-radius: 14px;
}
.task-hero h1,
.sample-head h1 {
  font-size: 23px;
  margin: 0 0 12px;
}
.task-hero p,
.sample-head p {
  color: #7b8985;
  margin: 0;
  font-size: 13px;
}
.task-hero > .badge {
  margin-left: auto;
}
.sample-head > div {
  flex: 1;
}
.task-kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding: 9px 10px;
  border: 1px solid #dfe8e5;
  border-radius: 14px;
  background: linear-gradient(120deg, #fff, #f6faf8);
  margin: 24px 0;
}
.task-kpis article {
  padding: 0 18px;
  border-right: 1px solid #e9eeeb;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.task-kpis article:last-child {
  border: 0;
}
.task-kpis span {
  font-size: 13px;
  color: #718478;
}
.task-kpis b {
  font-size: 20px;
  font-weight: 400;
}
.task-kpis small {
  font-size: 13px;
  font-weight: 400;
  color: #8b9b92;
}
.green {
  color: #00a486;
}
.violet {
  color: #7b4cdc;
}
.result-filters select {
  width: 160px;
}
.export {
  margin-left: auto;
}
.sample-results-scroll {
  max-width: 100%;
  max-height: 860px;
  overflow: auto;
}
.sample-results-scroll:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
.sample-table {
  min-width: calc(470px + var(--score-column-count) * 80px);
  table-layout: fixed;
  border-collapse: separate;
  border-spacing: 0;
}
.sample-input-column {
  width: 180px;
}
.sample-scene-column {
  width: 80px;
}
.sample-status-column,
.sample-score-column {
  width: 60px;
}
.sample-action-column {
  width: 90px;
}
.sample-table th,
.sample-table td {
  padding: 10px 8px;
}
.sample-table th {
  position: sticky;
  top: 0;
  z-index: 2;
  font-size: 13px;
  line-height: 1.4;
  white-space: normal;
  overflow-wrap: anywhere;
}
.sample-table th:last-child,
.sample-table td:last-child {
  position: sticky;
  right: 0;
  background: white;
  z-index: 1;
  box-shadow: -6px 0 10px #253a2e05;
}
.sample-table th:last-child {
  z-index: 3;
  background: #f9fafb;
}
.sample-name,
.sample-scenario {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.query-preview {
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: #8b9992;
  margin-bottom: 0;
}
.config-cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 22px;
  margin-top: 24px;
}
.config-block {
  background: #f7f9f8;
  border-radius: 10px;
  padding: 20px;
  margin: 18px 0;
  overflow-wrap: anywhere;
}
.config-cards h2 {
  font-size: 19px;
}
.config-block p,
.config-cards p {
  font-size: 13px;
  color: #6d7e75;
}
.config-block dt {
  font-weight: 600;
  margin-top: 12px;
}
.config-block dd {
  margin: 6px 0;
  color: #718478;
  font-size: 13px;
}
.full {
  grid-column: 1/-1;
}
.conversation-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(372px, 1.2fr);
  gap: 20px;
}
.conversation-panel,
.evaluation-panel {
  border: 1px solid #dfe8e5;
  background: white;
  border-radius: 14px;
  overflow: hidden;
}
.conversation-panel > header {
  padding: 18px 20px;
  border-bottom: 1px solid #e8edea;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.conversation-panel h2,
.evaluation-panel h2 {
  font-size: 18px;
  margin: 0;
}
.conversation-body {
  display: grid;
  grid-template-columns: 170px minmax(0, 1fr);
  height: 65vh;
  min-height: 360px;
  min-width: 0;
  overflow: hidden;
}
.conversation-body > aside {
  padding: 16px;
  background: #f8faf9;
  border-right: 1px solid #e8edea;
  overflow: auto;
}
.conversation-body > aside button {
  border: 0;
  border-radius: 8px;
  background: transparent;
  text-align: left;
  padding: 14px;
  width: 100%;
  margin-bottom: 10px;
  color: #496659;
}
.conversation-body > aside button.active {
  background: #e3f5ee;
  color: #008670;
}
.conversation-body > aside span {
  display: block;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 120px;
  font-size: 11px;
  margin-top: 10px;
}
.conversation-body > main {
  padding: 22px;
  overflow: auto;
  min-height: 0;
  min-width: 0;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}
.message {
  padding: 18px;
  background: #f5f7f6;
  border: 1px solid #e6ebe8;
  border-radius: 10px;
}
.expected {
  border-style: dashed;
  background: #fcfdfc;
}
.answer {
  background: #eff9f4;
}
.conversation-turn h4 {
  color: #687f73;
  font-size: 13px;
}
.conversation-turn pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 13px;
  line-height: 1.8;
}
.evaluation-panel {
  padding: 22px;
  max-height: calc(75vh + 80px);
  overflow: auto;
}
.secondary.selected {
  background: #e4f7ee;
  color: #00836c;
}
summary {
  cursor: pointer;
}
@media (max-width: 1100px) {
  .task-kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
  }
  .task-kpis article:nth-child(2n) {
    border-right: 0;
  }
  .conversation-layout {
    grid-template-columns: 1fr;
  }
  .evaluation-panel {
    max-height: none;
  }
}
@media (max-width: 700px) {
  .config-cards {
    grid-template-columns: 1fr;
  }
  .task-hero {
    padding: 10px 16px;
    gap: 12px;
    flex-wrap: wrap;
  }
  .task-hero h1 {
    font-size: 18px;
  }
  .hero-icon {
    display: none;
  }
  .conversation-body {
    grid-template-columns: 110px 1fr;
  }
  .conversation-body > aside {
    padding: 6px;
  }
  .conversation-body > main {
    padding: 12px;
  }
  .sample-head {
    flex-wrap: wrap;
  }
  .task-kpis {
    grid-template-columns: repeat(2, 1fr);
  }
  .result-filters {
    flex-wrap: wrap;
  }
}
</style>
