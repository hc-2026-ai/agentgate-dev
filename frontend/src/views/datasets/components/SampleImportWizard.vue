<script setup lang="ts">
import { computed, ref } from 'vue';
import SampleEditor from './SampleEditor.vue';
import { datasetApi } from '../../../api/datasets';
import { request } from '../../../api/evaluations';
import { annotationTasks } from '../../../stores/review-assets';
import { parseSampleFile, blankSample, sampleFileFormats } from '../utils/sample-import';
import importTemplateUrl from '../../../assets/dataset-import-template.xlsx?url';
import jsonTemplateUrl from '../../../assets/dataset-import-template.json?url';
import csvTemplateUrl from '../../../assets/dataset-import-template.csv?url';
import type { EvaluationCase } from '../types/index';
import type { Trace } from '../../../api/client';
import type { EvaluationRun } from '../../evaluation/types/run';
const props = defineProps<{ datasetId: string; datasetName: string }>();
const acceptedFiles = sampleFileFormats.map((format) => '.' + format.value).join(',');
const formatDescription = sampleFileFormats.map((format) => format.label).join(' / ');
const jsonExample = JSON.stringify(
  {
    cases: [
      {
        name: '单轮问答示例',
        category: 'positive',
        difficulty: 'medium',
        tags: ['示例'],
        notes: '',
        initial_state: {},
        turns: [
          {
            input: { txt: '你好' },
            expectations: [
              {
                name: '回答内容',
                kind: 'output',
                path: null,
                condition: { kind: 'equals', expected: '你好，请问有什么可以帮您？' },
              },
            ],
            notes: '',
          },
        ],
      },
    ],
  },
  null,
  2,
);
const emit = defineEmits<{ close: []; manual: []; imported: [] }>();
const method = ref('manual'),
  step = ref(1),
  busy = ref(false),
  error = ref(''),
  items = ref<EvaluationCase[]>([]),
  taskId = ref(''),
  selected = ref<string[]>([]),
  saved = ref(0);
const task = computed(() =>
  annotationTasks.value.find((t) => t.id === taskId.value && !t.deletedAt),
);
const sessions = computed(() => [
  ...new Set(
    Object.keys(task.value?.annotations ?? {}).map((k) => k.split('/').slice(0, 2).join('/')),
  ),
]);
const done = new Set<string>();
async function downloadTemplate(url: string, format: string) {
  error.value = '';
  try {
    const response = await fetch(url);
    if (!response.ok) throw Error('模板下载失败，请稍后重试');
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = '测评集导入模板.' + format;
    link.click();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  } catch (e) {
    error.value = String(e);
  }
}
function reset() {
  items.value = [];
  error.value = '';
  saved.value = 0;
  done.clear();
}
const manualItem = ref<EvaluationCase>(blankSample(''));
const manualDirty = ref(false);
const manualEditor = ref<InstanceType<typeof SampleEditor> | null>(null);
function next() {
  reset();
  manualItem.value = blankSample('');
  manualDirty.value = false;
  step.value = 2;
}
async function saveManual(item: EvaluationCase) {
  items.value = [item];
  await save();
  if (step.value === 3) manualDirty.value = false;
}
function previous() {
  if (manualDirty.value && !window.confirm('放弃未保存的样本？')) return;
  manualDirty.value = false;
  step.value = 1;
  reset();
}
async function fileChanged(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  reset();
  if (!file) return;
  busy.value = true;
  try {
    items.value = await parseSampleFile(file);
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}
async function readAnnotations() {
  reset();
  if (!task.value || !selected.value.length) {
    error.value = '请选择标注模板和会话';
    return;
  }
  busy.value = true;
  try {
    const result: EvaluationCase[] = [];
    for (const key of selected.value) {
      const [run, caseId] = key.split('/');
      const trace = await request<Trace>(
        '/runs/' + encodeURIComponent(run) + '/traces/' + encodeURIComponent(caseId),
      );
      const report = await request<{ run: EvaluationRun }>('/runs/' + encodeURIComponent(run));
      const source = report.run.manifest.dataset.cases.find((c) => c.id === caseId);
      if (!source) throw Error('来源测评缺少样本快照，无法保留初始状态');
      const sample = blankSample(task.value.name + ' · ' + caseId.slice(0, 8));
      sample.initial_state = JSON.parse(JSON.stringify(source.initial_state));
      sample.category = source.category;
      sample.difficulty = source.difficulty;
      sample.tags = [...source.tags];
      sample.notes =
        '标注来源：模板 ' +
        task.value.id +
        '；测评 ' +
        run +
        '；样本 ' +
        caseId +
        '；Trace ' +
        trace.trace_id;
      sample.turns = source.turns.map((sourceTurn) => {
        const id = sourceTurn.id,
          outcome = trace.turn_outcomes?.[id];
        if (!outcome) throw Error('来源会话轮次缺少 Trace 输入，无法完整导入');
        const a = task.value!.annotations[key + '/' + id];
        if (!outcome.input || typeof outcome.input !== 'object' || Array.isArray(outcome.input))
          throw Error('Trace 输入不是对象，无法安全导入');
        const turn = blankSample().turns[0];
        turn.input = JSON.parse(JSON.stringify(outcome.input));
        if (a) {
          if (Object.values(a.scores).some((s) => s == null || !Number.isFinite(s)))
            throw Error('会话存在未完成评分');
          turn.notes =
            '来源轮次 ' + id + '；人工评分 ' + JSON.stringify(a.scores) + '；备注 ' + a.note;
          sample.tags.push(...a.tags);
          if (a.expected.trim())
            turn.expectations.push({
              id: crypto.randomUUID(),
              name: '人工期望输出',
              kind: 'output',
              path: null,
              condition: { kind: 'equals', expected: a.expected },
            });
        } else turn.notes = '来源轮次 ' + id + '；保留会话上下文，此轮未标注';
        return turn;
      });
      if (!sample.turns.length) throw Error('Trace 未提供轮次，无法导入');
      sample.tags = [...new Set(sample.tags)];
      result.push(sample);
    }
    items.value = result;
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}
async function save() {
  if (!items.value.length || busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    let draft = await datasetApi.currentDraft(props.datasetId);
    for (const item of items.value) {
      if (done.has(item.id)) continue;
      if (draft.cases.some((c) => c.id === item.id)) {
        done.add(item.id);
        saved.value = done.size;
        continue;
      }
      draft = await datasetApi.addCase(props.datasetId, item, draft.content_sha256);
      done.add(item.id);
      saved.value = done.size;
    }
    step.value = 3;
    emit('imported');
  } catch (e) {
    error.value =
      String(e) +
      '；已确认保存 ' +
      done.size +
      ' 条。未清除成功记录，可重试；重试先核对服务器样本ID。';
  } finally {
    busy.value = false;
  }
}
function close() {
  if (busy.value) return;
  if (manualDirty.value && !window.confirm('放弃未保存的样本？')) return;
  emit('close');
}
</script>
<template>
  <el-dialog
    :model-value="true"
    title="新增样本"
    class="sample-wizard-dialog"
    width="min(800px,96vw)"
    :close-on-click-modal="false"
    :show-close="!busy"
    :close-on-press-escape="!busy"
    :before-close="close"
  >
    <div class="sample-wizard">
      <div class="steps">
        <b :class="{ active: step === 1 }">1 选择方式</b><span>—</span
        ><b :class="{ active: step === 2 }">2 配置填写</b><span>—</span
        ><b :class="{ active: step === 3 }">3 完成</b>
      </div>
      <h3>{{ datasetName }} · 新增样本</h3>
      <section v-if="method === 'file' && step === 2" class="import-template">
        <div class="template-downloads">
          <a
            :href="importTemplateUrl"
            download="测评集导入模板.xlsx"
            class="template-download"
            @click.prevent="downloadTemplate(importTemplateUrl, 'xlsx')"
          >
            下载测评集模板（Excel）
          </a>
          <a
            :href="jsonTemplateUrl"
            download="测评集导入模板.json"
            class="template-download"
            @click.prevent="downloadTemplate(jsonTemplateUrl, 'json')"
          >
            下载测评集模板（JSON）
          </a>
          <a
            :href="csvTemplateUrl"
            download="测评集导入模板.csv"
            class="template-download"
            @click.prevent="downloadTemplate(csvTemplateUrl, 'csv')"
          >
            下载测评集模板（CSV）
          </a>
        </div>
        <p>
          三个模板均包含相同的单轮、多轮及多种测评项示例。Excel
          另附“填写说明”和“测评项示例”工作表，说明表不会导入。
        </p>
        <p>测评集名称和关联 Agent 在创建页面填写；模板中的输入和测评项需与所选 Agent 匹配。</p>
        <details>
          <summary>JSON 格式说明与示例</summary>
          <p>
            使用 UTF-8 编码，顶层为 {"cases": [...]}，cases
            中每项是一个样本。使用双引号，不要添加注释或末尾多余逗号。
          </p>
          <p>
            必填：name（样本名称）、turns（至少一轮）。每轮必填 input（输入对象）和
            expectations（测评项数组，无规则检查时填 []）。多轮对话按 turns 数组顺序执行。
          </p>
          <p>
            选填：category 为 positive / negative / boundary，默认 positive；difficulty 为 easy /
            medium / hard，默认 medium；tags 为标签数组，initial_state 为初始状态对象，notes
            为备注。轮次也可填写 notes。
          </p>
          <p>
            expectations 的 kind 支持
            output（输出）、state（状态）、tool_argument（工具参数）、tool_call（工具调用）、skill_route（技能路由）、policy（策略）、execution_path（执行路径）。各类型所需字段可参考下载模板和
            Excel 的“测评项示例”。
          </p>
          <p>
            JSON 中 input、initial_state 直接填写对象，expectations、tags
            直接填写数组，不要转成字符串。示例中的 txt 应替换为 Agent 实际接收的字段；LLM
            评估器在测评任务中单独选择。
          </p>
          <pre>{{ jsonExample }}</pre>
        </details>
        <details>
          <summary>Excel / CSV 字段填写说明</summary>
          <p>
            Excel 填写 Cases 表；CSV 使用 UTF-8 编码和英文逗号分列。每行一轮对话，相同 case_id
            合并为一个样本，case_name 必须一致；turn_order 填不重复的正整数并决定轮次顺序。
          </p>
          <p>
            必填：case_name（样本名称）、input_json（输入对象的 JSON 文本）。case_id
            留空时每行创建独立样本。
          </p>
          <p>
            测评项：expectations_json 填 JSON 数组，无规则检查时填
            []。一轮可包含多个不同类型的测评项。
          </p>
          <p>
            选填：category（positive / negative / boundary，默认 positive）、difficulty（easy /
            medium / hard，默认
            medium）、tags_json（标签数组）、case_notes（样本备注）、initial_state_json（初始状态对象）、turn_id（轮次标识）、turn_notes（轮次备注）。同一样本的这些样本级字段请保持一致。
          </p>
          <p>
            CSV
            字段包含逗号、换行或双引号时，用双引号包裹整个字段，内部双引号写成两个双引号。建议用表格软件编辑后保存为
            CSV UTF-8；也支持简易列 case_name、query、expected（可选）。
          </p>
          <p>导入时会生成新的样本、轮次和测评项 ID；重复导入会新增样本。</p>
        </details>
      </section>
      <div v-if="step === 1" class="methods">
        <button
          v-for="m in [
            { id: 'file', name: '⇧ 导入测评集', description: formatDescription },
            { id: 'manual', name: '＋ 手动创建', description: '多轮对话、期望输出与工具调用' },
            {
              id: 'annotation',
              name: '✓ 从标注导入',
              description: '选择标注模板，再选择已标注会话',
            },
          ]"
          :key="m.id"
          :class="{ selected: method === m.id }"
          :disabled="m.id === 'annotation'"
          @click="method = m.id"
        >
          <strong>{{ m.name }}</strong
          ><span>{{ m.description }}</span></button
        ><button disabled><strong>✦ AI 生成数据</strong><span>生成接口尚未接通</span></button>
      </div>
      <template v-if="step === 2">
        <SampleEditor
          v-if="method === 'manual'"
          ref="manualEditor"
          hide-save-button
          :item="manualItem"
          :editable="true"
          :saving="busy"
          :persisted="false"
          :validation-issues="[]"
          @save="saveManual"
          @dirty-change="manualDirty = $event"
        />
        <template v-else-if="method === 'file'"
          ><label
            >选择文件<input
              type="file"
              :accept="acceptedFiles"
              aria-label="样本导入文件"
              :disabled="busy || saved > 0"
              @change="fileChanged"
          /></label>
          <p>
            支持 Excel (.xlsx)、JSON、CSV，也可直接上传导出的样本文件。文件不超过 10MB，最多 1000
            个样本；Excel / CSV 最多 2000 行数据。
          </p></template
        >
        <template v-else
          ><label
            >标注模板<select
              v-model="taskId"
              aria-label="导入标注模板"
              :disabled="busy || saved > 0"
              @change="
                selected = [];
                reset();
              "
            >
              <option value="">请选择标注模板</option>
              <option
                v-for="t in annotationTasks.filter((t) => !t.deletedAt)"
                :key="t.id"
                :value="t.id"
              >
                {{ t.name }} · {{ t.app.external_target_id }}
              </option>
            </select></label
          >
          <p>
            读取当前页面已保存的标注；独立标注服务尚未接通。导入后的样本会保存到数据库，标注源本身仍为页面内存。
          </p>
          <label v-for="key in sessions" :key="key" class="session"
            ><input
              type="checkbox"
              v-model="selected"
              :value="key"
              :disabled="busy || saved > 0"
              @change="reset()"
            />测评 / 会话：{{ key }}</label
          >
          <p v-if="!sessions.length">暂无已保存的标注会话，请先在标注中完成评分并保存。</p>
          <button
            class="secondary"
            :disabled="!selected.length || busy || saved > 0"
            @click="readAnnotations"
          >
            读取所选会话
          </button></template
        >
        <p v-if="busy" role="status">正在处理，请勿关闭…</p>
        <table v-if="items.length && method !== 'manual'">
          <thead>
            <tr>
              <th>样本名称</th>
              <th>轮次</th>
              <th>期望检查</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.id">
              <td>{{ item.name }}</td>
              <td>{{ item.turns.length }}</td>
              <td>
                {{
                  item.turns.reduce(
                    (n, t) =>
                      n +
                      t.expectations.length +
                      t.required_tools.length +
                      t.forbidden_tools.length +
                      t.policy_rules.length +
                      Number(Boolean(t.expected_skill)) +
                      (t.preserved_skill_routes?.length ?? 0) +
                      (t.original_expectations?.filter((e) => e.kind === 'execution_path').length ??
                        0),
                    0,
                  )
                }}
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="items.length && method !== 'manual'">
          预览
          {{ items.length }}
          条；导入只新增草稿样本，不覆盖已有样本、不自动发布。批量接口尚未提供，逐条写入并报告成功数量。
        </p>
      </template>
      <div v-if="step === 3" class="complete">
        <h2>✓ {{ method === 'manual' ? '创建完成' : '导入完成' }}</h2>
        <p>{{ saved }} 条样本已保存到数据库草稿，可继续管理、编辑和发布。</p>
      </div>
      <p v-if="error" role="alert" class="error">{{ error }}</p>
    </div>
    <template #footer
      ><div class="wizard-actions">
        <button class="secondary" :disabled="busy" @click="close">
          {{ step === 3 ? '返回数据样本' : '取消' }}</button
        ><button v-if="step === 1" class="primary" @click="next">下一步</button
        ><template v-if="step === 2"
          ><button class="secondary" :disabled="busy || saved > 0" @click="previous">上一步</button
          ><button
            v-if="method === 'manual'"
            class="primary"
            :disabled="busy || manualEditor?.fetchingVariables"
            :title="manualEditor?.fetchingVariables ? '正在获取初始化变量，请稍候' : ''"
            @click="manualEditor?.save()"
          >
            {{ busy ? '保存中…' : '保存样本' }}</button
          ><button v-else class="primary" :disabled="busy || !items.length" @click="save">
            {{ saved ? '继续导入' : '确认导入到草稿' }}
          </button></template
        >
      </div></template
    >
  </el-dialog>
</template>
<style scoped>
.import-template {
  padding: 14px 18px;
  margin: 14px 0;
  border: 1px solid #cce9e3;
  border-radius: 8px;
  background: #f2faf7;
}
.template-download {
  color: #008b76;
  font-weight: 600;
}
.template-downloads {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 20px;
}
.import-template details {
  margin-top: 12px;
  font-size: 13px;
}
.import-template summary {
  cursor: pointer;
  color: #008b76;
  font-weight: 600;
}
.import-template pre {
  max-height: 300px;
  overflow: auto;
  padding: 12px;
  border-radius: 6px;
  background: #fff;
}
.import-template p {
  margin: 8px 0 0;
  font-size: 13px;
  line-height: 1.6;
}
.wizard-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.wizard-actions button + button {
  margin-left: 0;
}
.wizard-actions button {
  flex-shrink: 0;
}
.sample-wizard {
  color: #344158;
}
.sample-wizard h3 {
  margin: 10px 0 14px;
}
.steps {
  display: flex;
  justify-content: center;
  gap: 20px;
  padding: 14px 0;
  color: #99a3b4;
  font-size: 14px;
}
.steps .active {
  color: #008b76;
}
.methods {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.methods button {
  display: flex;
  flex-direction: column;
  gap: 10px;
  text-align: left;
  padding: 16px 24px;
  min-height: 104px;
  border: 1px solid #dce4ef;
  border-radius: 10px;
  background: white;
  color: #41516d;
}
.methods button.selected {
  border-color: #008b76;
  background: #f3f6ff;
}
.methods strong {
  font-size: 17px;
}
.methods span {
  font-size: 13px;
}
.methods button:disabled {
  opacity: 0.5;
}
.sample-wizard p {
  font-size: 12px;
  color: #7b8aa0;
  line-height: 1.8;
}
.sample-wizard label {
  display: block;
  margin: 14px 0;
}
.sample-wizard select,
.sample-wizard input[type='file'] {
  display: block;
  width: 100%;
  padding: 12px;
  border: 1px solid #dce4ef;
  border-radius: 8px;
  margin-top: 8px;
}
.session {
  font-size: 12px;
  overflow-wrap: anywhere;
}
.sample-wizard table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 20px;
}
th,
td {
  padding: 12px;
  border-bottom: 1px solid #e4eaf2;
  text-align: left;
  font-size: 13px;
}
.error {
  color: #c33 !important;
}
.complete {
  text-align: center;
  padding: 30px;
}
@media (max-width: 760px) {
  .wizard-actions button {
    padding: 0 10px;
  }
  .methods {
    grid-template-columns: 1fr;
  }
  .steps {
    gap: 8px;
    padding: 16px 0;
  }
}
</style>
