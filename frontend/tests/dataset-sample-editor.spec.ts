import { test, expect, type Locator, type Page, type Route } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import vue from '@vitejs/plugin-vue';
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

// Real components/API converters with private SQLite persistence.
let server: ViteDevServer;
let backend: ChildProcess;
let temporaryDirectory: string;
let baseUrl: string;
const frontendRoot = fileURLToPath(new URL('..', import.meta.url));
const repositoryRoot = fileURLToPath(new URL('../..', import.meta.url));
const runtimeErrors = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const errors: string[] = [];
  runtimeErrors.set(page, errors);
  page.on('pageerror', error => errors.push(error.message));
});
test.afterEach(async ({ page }) => {
  expect(runtimeErrors.get(page)).toEqual([]);
  await expect(page.locator('vite-error-overlay')).toHaveCount(0);
});
const fixture = `<!doctype html><html><head><title>Sample editor acceptance</title></head>
<body><div id="app"></div><script type="module">
import { createApp, h, ref, onMounted } from 'vue';
import { createPinia } from 'pinia';
import { useAuthStore } from '/src/stores/modules/auth.ts';
import ElementPlus from 'element-plus';
import SampleEditor from '/src/views/datasets/components/SampleEditor.vue';
import SampleImportWizard from '/src/views/datasets/components/SampleImportWizard.vue';
import { datasetApi } from '/src/api/datasets.ts';
import { blankSample } from '/src/views/datasets/utils/sample-import.ts';
import '/node_modules/element-plus/dist/index.css';
import '/src/styles/revision.scss';
const datasetId = new URLSearchParams(location.search).get('dataset');
createApp({ setup() {
  const auth = useAuthStore();
  auth.loginExternal();
  const item = ref(null), draft = ref(null), editable = ref(true), saving = ref(false), error = ref(''), dirty = ref(false), wizardOpen = ref(false);
  async function load() {
    draft.value = new URLSearchParams(location.search).has('published')
      ? (await datasetApi.versions(datasetId)).find(version => version.status === 'published')
      : await datasetApi.currentDraft(datasetId);
    editable.value = draft.value.status === 'draft';
    item.value = draft.value.cases[0] ?? blankSample('Acceptance sample');
  }
  async function save(value) {
    saving.value = true;
    try {
      const exists = draft.value.cases.some(c => c.id === value.id);
      draft.value = await datasetApi[exists ? 'updateCase' : 'addCase'](datasetId, value, draft.value.content_sha256);
      item.value = draft.value.cases.find(c => c.id === value.id);
    } catch (e) { error.value = String(e); }
    finally { saving.value = false; }
  }
  async function publish() {
    try {
      draft.value = await datasetApi.publish(datasetId, draft.value.content_sha256);
      item.value = draft.value.cases[0]; editable.value = false;
      history.replaceState(null, '', location.pathname + '?dataset=' + datasetId + '&published=1');
    } catch (e) { error.value = String(e); }
  }
  onMounted(() => load().catch(e => error.value = String(e)));
  return () => h('main', { class: 'revision-app' }, [
    h('h1', 'Sample editor acceptance'),
    h('button', { onClick: () => auth.loginBank('sample-test-token', 'team-test', '测试团队') }, 'Switch to bank'),
    h('button', { onClick: () => auth.loginExternal() }, 'Switch to external'),
    h('button', { onClick: () => auth.initializePlatform({ token: 'sample-integrated-token', teamId: 'integrated-team',
      teamName: '集成测试团队', userId: 'integrated-user', userName: '集成用户' }) }, 'Switch to integrated'),
    h('span', { 'data-testid': 'dirty' }, String(dirty.value)),
    h('p', { role: 'alert' }, error.value),
    h('button', { onClick: load }, 'Reload server sample'),
    h('button', { onClick: () => wizardOpen.value = true }, 'Open manual sample wizard'),
    h(SampleEditor, { item: item.value, editable: editable.value, saving: saving.value, persisted: !!draft.value?.cases.length,
      onSave: save, onDirtyChange: value => dirty.value = value }),
    h('button', { onClick: publish }, 'Publish acceptance draft'),
    wizardOpen.value ? h(SampleImportWizard, { datasetId, datasetName: 'Acceptance dataset',
      onClose: () => wizardOpen.value = false, onImported: load }) : null,
  ]);
} }).use(createPinia()).use(ElementPlus).mount('#app');
</script></body></html>`;

test.beforeAll(async () => {
  temporaryDirectory = await mkdtemp(join(tmpdir(), 'agentgate-sample-editor-'));
  // Importing server.app creates its factory app, so set DB/log paths before import.
  const script = `import socket, uvicorn
from agentgate.server.app import app
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
print('ACCEPTANCE_PORT=' + str(sock.getsockname()[1]), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[sock])`;
  const python = process.env.AGENTGATE_TEST_PYTHON ?? join(repositoryRoot, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin', process.platform === 'win32' ? 'python.exe' : 'python');
  backend = spawn(python, ['-B', '-u', '-c', script], {
    cwd: temporaryDirectory,
    env: {
      PATH: process.env.PATH, SystemRoot: process.env.SystemRoot, TEMP: temporaryDirectory,
      PYTHONPATH: join(repositoryRoot, 'src'), AGENTGATE_DB_TYPE: 'sqlite',
      AGENTGATE_DB: join(temporaryDirectory, 'acceptance.db'),
      AGENTGATE_LOG_PATH: join(temporaryDirectory, 'logs'),
    },
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  const apiUrl = await new Promise<string>((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('Private API did not start: ' + output.slice(-2000))), 25000);
    backend.on('error', error => { clearTimeout(timeout); reject(error); });
    backend.on('exit', code => { clearTimeout(timeout); reject(Error(`Private API exited ${code}: ${output.slice(-2000)}`)); });
    backend.stderr!.on('data', chunk => { output += chunk.toString(); });
    backend.stdout!.on('data', chunk => {
      output += chunk.toString();
      const match = /ACCEPTANCE_PORT=(\d+)/.exec(output);
      if (match) { clearTimeout(timeout); resolve('http://127.0.0.1:' + match[1]); }
    });
  });
  await expect.poll(async () => {
    try { return (await fetch(apiUrl + '/api/datasets')).status; } catch { return 0; }
  }).toBe(200);
  server = await createServer({
    configFile: false, root: frontendRoot, cacheDir: join(temporaryDirectory, 'vite-cache'),
    define: {
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/api'),
      'import.meta.env.VITE_AGENT_PLATFORM_ORIGIN': JSON.stringify('http://bank.invalid'),
      'import.meta.env.VITE_ABCCLAW_PLATFORM_ORIGIN': JSON.stringify('http://bank.invalid'),
      'import.meta.env.VITE_LOCAL_PLATFORM_ORIGIN': JSON.stringify('http://127.0.0.1:8119'),
    },
    plugins: [vue(), { name: 'sample-editor-acceptance', configureServer(instance) {
      instance.middlewares.use('/__sample-editor', async (_req, res, next) => {
        try {
          res.setHeader('Content-Type', 'text/html');
          res.end(await instance.transformIndexHtml('/__sample-editor', fixture));
        } catch (error) { next(error); }
      });
    } }],
    server: { host: '127.0.0.1', port: 0, proxy: { '/api': apiUrl } },
  });
  await server.listen();
  const address = server.httpServer!.address();
  if (!address || typeof address === 'string') throw Error('No acceptance UI server');
  baseUrl = `http://127.0.0.1:${address.port}`;
});

test.afterAll(async () => {
  await server?.close();
  if (backend?.pid && backend.exitCode === null && backend.signalCode === null) {
    await new Promise<void>((resolve, reject) => {
      const finish = (error?: Error) => {
        clearTimeout(timeout);
        backend.removeListener('close', exited);
        backend.removeListener('exit', exited);
        if (error) reject(error);
        else resolve();
      };
      const exited = () => finish();
      const timeout = setTimeout(() => finish(Error('Private API did not terminate within 5 seconds')), 5000);
      timeout.unref();
      backend.once('close', exited);
      backend.once('exit', exited);
      try { backend.kill(); } catch (error) { finish(error as Error); }
    });
  }
  if (temporaryDirectory) await rm(temporaryDirectory, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
});

type Kind = 'state' | 'tool_argument';
function sample(kind: Kind) {
  return {
    id: 'acceptance-case', name: 'Preserved sample', category: 'negative', difficulty: 'hard',
    initial_state: { ticket: 'existing', attempts: 0 }, tags: ['retained'], notes: 'case notes',
    turns: [{ id: 'acceptance-turn', input: { query: '输入' }, notes: 'turn notes', expectations: [
      { id: 'editable-rule', name: 'Named rule', kind, path: 'status',
        ...(kind === 'tool_argument' ? { tool: 'lookup', occurrence: 'any' } : {}),
        condition: { kind: 'equals', expected: 'abcd' } },
      { id: 'other-rule', name: 'Retained policy', kind: 'policy', policy_id: 'approval_required' },
    ] }],
  };
}

async function open(page: Page, kind?: Kind) {
  const created = await page.request.post(baseUrl + '/api/datasets', { data: { name: 'Sample editor acceptance' } });
  expect(created.status()).toBe(201);
  const datasetId = (await created.json()).data.dataset.id as string;
  if (kind) {
    const seeded = await page.request.post(baseUrl + `/api/datasets/${datasetId}/drafts/cases`, { data: sample(kind) });
    expect(seeded.status()).toBe(201);
  }
  await page.goto(baseUrl + '/__sample-editor?dataset=' + datasetId);
  await expect(page.getByLabel('样本名称', { exact: true })).toBeVisible();
  return datasetId;
}

function expectationRow(page: Page, index = 0) {
  return page.getByTestId(`expectation-${index}`);
}

async function namedExpectation(page: Page, name: string) {
  const rows = page.locator('.expectation-row');
  const names = await rows.getByPlaceholder('检查名称（可选）').evaluateAll(
    inputs => inputs.map(input => (input as HTMLInputElement).value),
  );
  const index = names.indexOf(name);
  expect(index, `Expected a visible rule named ${name}`).toBeGreaterThanOrEqual(0);
  return rows.nth(index);
}

function expectationInput(page: Page, field: 'value' | 'path' | 'tool' | 'allowed' | 'schema', index = 0) {
  return page.getByTestId(`expectation-${field}-${index}`);
}

function valueInput(page: Page, _kind: Kind) {
  return expectationInput(page, 'value');
}

function editorText(_kind: Kind, expected: unknown) {
  return JSON.stringify(expected);
}

async function addExpectation(page: Page, kind: Kind | 'output') {
  const index = await page.locator('.expectation-row').count();
  await page.getByTestId('add-expectation').click();
  await page.getByRole('menuitem', {
    name: { state: '最终状态', tool_argument: '工具参数', output: '最终输出' }[kind], exact: true,
  }).click();
  await expect(page.locator('.expectation-row')).toHaveCount(index + 1);
  return index;
}

async function selectCondition(page: Page, index: number, label: string) {
  await expectationRow(page, index).locator('.expectation-fields .el-select').last().click();
  await page.getByRole('option', { name: label, exact: true }).click();
}

async function typeRaw(input: Locator, text: string) {
  await input.press('ControlOrMeta+A');
  await input.press('Backspace');
  await expect(input).toHaveValue('');
  let prefix = '';
  for (const character of text) {
    await input.pressSequentially(character);
    prefix += character;
    await expect(input).toHaveValue(prefix);
  }
}

async function save(page: Page, datasetId: string, method = 'PUT') {
  const response = page.waitForResponse(response => response.request().method() === method &&
    response.url().includes(`/api/datasets/${datasetId}/drafts/cases`));
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  expect((await response).ok()).toBeTruthy();
  await expect(page.getByTestId('dirty')).toHaveText('false');
  const saved = await page.request.get(baseUrl + `/api/datasets/${datasetId}/drafts/current`);
  expect(saved.ok()).toBeTruthy();
  return (await saved.json()).data.cases[0];
}

async function expandInitialization(page: Page | Locator) {
  const section = page.locator('details.case-initialization');
  await expect(section.locator('summary')).toHaveText('初始变量');
  if (!(await section.evaluate(element => (element as HTMLDetailsElement).open))) {
    await section.locator('summary').click();
  }
}

async function addInitializationVariable(page: Page, name: string, value: string) {
  await expandInitialization(page);
  const index = await page.getByLabel(/^初始化变量名 \d+$/).count() + 1;
  await page.getByRole('button', { name: '添加变量', exact: true }).click();
  await page.getByLabel(`初始化变量名 ${index}`, { exact: true }).fill(name);
  await page.getByLabel(`初始化变量值 ${index}`, { exact: true }).fill(value);
}

test('workflow Case initialization and complete JSON turns persist and publish', async ({ page }) => {
  const datasetId = await open(page);
  const variables = [{ name: 'centreId', value: '810000', type: 'str' },
    { name: 'userId', value: 'hk_yat', type: 'str' },
    { name: 'bankNo', value: '810000', type: 'str' }];
  const first = { user_input_text: '我们行的年休假政策是什么', menuId: 'KNOWLEDGE_QUERY' };
  const second = { user_input_text: '第二轮', collected_data: {},
    form: { values: [0, false, null, { amount: 12.5 }] } };
  await expect(page.getByText('初始变量', { exact: true })).toBeVisible();
  await expect(page.getByLabel(/^初始化变量名 \d+$/)).toHaveCount(0);
  for (const variable of variables) {
    await addInitializationVariable(page, variable.name, variable.value);
  }
  for (const index of variables.keys()) {
    await expect(page.getByLabel(`初始化变量类型 ${index + 1}`, { exact: true })).toHaveText('str');
    await expect(page.getByLabel(`初始化变量是否必填 ${index + 1}`, { exact: true })).toHaveText('-');
  }
  await expect(page.locator('details.case-initialization select')).toHaveCount(0);
  await expect(page.getByTestId('dirty')).toHaveText('true');
  await page.getByLabel('结构化 JSON 输入', { exact: true }).check();
  await page.getByRole('textbox', { name: '用户输入 JSON', exact: true }).fill(JSON.stringify(first));
  await page.getByRole('button', { name: '添加新轮次' }).click();
  await page.getByLabel('结构化 JSON 输入', { exact: true }).check();
  await page.getByRole('textbox', { name: '用户输入 JSON', exact: true }).fill(JSON.stringify(second));
  const saved = await save(page, datasetId, 'POST');
  expect(saved.initial_state).toEqual({ config_variables: variables });
  for (const variable of saved.initial_state.config_variables) {
    expect(variable).not.toHaveProperty('required');
  }
  expect(saved.turns.map((turn: any) => turn.input)).toEqual([first, second]);
  await page.reload();
  await expandInitialization(page);
  for (const [index, variable] of variables.entries()) {
    await expect(page.getByLabel(`初始化变量名 ${index + 1}`, { exact: true })).toHaveValue(variable.name);
    await expect(page.getByLabel(`初始化变量值 ${index + 1}`, { exact: true })).toHaveValue(variable.value);
    await expect(page.getByLabel(`初始化变量类型 ${index + 1}`, { exact: true })).toHaveText('str');
    await expect(page.getByLabel(`初始化变量是否必填 ${index + 1}`, { exact: true })).toHaveText('-');
  }
  await expect(page.getByRole('button', { name: '对话预览', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('初始化变量值 1', { exact: true })).toBeEnabled();
  const published = page.waitForResponse(response => response.url().endsWith('/drafts/publish'));
  await page.getByRole('button', { name: 'Publish acceptance draft' }).click();
  expect((await published).ok()).toBeTruthy();
  for (const index of variables.keys()) {
    await expect(page.getByLabel(`初始化变量名 ${index + 1}`, { exact: true })).toBeDisabled();
    await expect(page.getByLabel(`初始化变量值 ${index + 1}`, { exact: true })).toBeDisabled();
  }
  await expect(page.getByRole('button', { name: '添加变量', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^删除初始化变量 \d+$/ })).toHaveCount(0);
  const response = await page.request.get(baseUrl + `/api/datasets/${datasetId}/versions/1`);
  expect((await response.json()).data.cases[0].initial_state).toEqual({ config_variables: variables });
  const exported = await page.request.get(baseUrl + `/api/datasets/${datasetId}/versions/1/export`);
  expect(exported.ok()).toBeTruthy();
  expect((await exported.json()).data.version.cases[0].initial_state).toEqual({ config_variables: variables });
  await test.info().attach('workflow-case-inputs', {
    body: await page.screenshot({ fullPage: true }), contentType: 'image/png',
  });
});

test('workflow initialization starts collapsed and keeps values when collapsed and saved', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const section = page.locator('details.case-initialization');
  const summary = section.locator('summary');
  await expect(summary).toHaveText('初始变量');
  await expect(section).toHaveJSProperty('open', false);
  const spacing = await section.evaluate(element => {
    const bounds = element.getBoundingClientRect();
    const heading = element.querySelector('summary')!.getBoundingClientRect();
    return { top: heading.top - bounds.top, bottom: bounds.bottom - heading.bottom - 1 };
  });
  expect(spacing.top).toBeGreaterThanOrEqual(12);
  expect(Math.abs(spacing.top - spacing.bottom)).toBeLessThanOrEqual(1);
  await expect(summary).toHaveCSS('color', 'rgb(0, 127, 105)');
  await expect(summary).toHaveCSS('font-size', '12px');
  await expect(summary).toHaveCSS('font-weight', '400');
  await expect(summary).toHaveCSS('display', 'list-item');
  await expect(summary).toHaveCSS('list-style-type', 'disclosure-closed');
  await summary.press('Enter');
  await expect(section).toHaveJSProperty('open', true);
  await addInitializationVariable(page, 'centreId', '810000');
  await summary.click();
  await expect(section).toHaveJSProperty('open', false);
  await expect(page.getByLabel('初始化变量值 1', { exact: true })).toBeHidden();
  await test.info().attach('workflow-initialization-collapsed', {
    body: await page.screenshot({ fullPage: true }), contentType: 'image/png',
  });
  expect((await save(page, datasetId)).initial_state).toEqual({
    ...sample('state').initial_state, config_variables: [{ name: 'centreId', value: '810000', type: 'str' }],
  });
  await page.reload();
  await expect(summary).toBeVisible();
  await expect(section).toHaveJSProperty('open', false);
  await summary.press('Enter');
  await expect(page.getByLabel('初始化变量名 1', { exact: true })).toHaveValue('centreId');
  await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('810000');
  await test.info().attach('workflow-initialization-expanded', {
    body: await page.screenshot({ fullPage: true }), contentType: 'image/png',
  });
});

test('workflow initialization is optional and ignores completely blank rows', async ({ page }) => {
  const datasetId = await open(page);
  await page.getByLabel('用户输入', { exact: true }).fill('无需初始化变量的输入');
  expect((await save(page, datasetId, 'POST')).initial_state).toEqual({});
  await addInitializationVariable(page, '', '');
  await addInitializationVariable(page, '  ', '');
  expect((await save(page, datasetId)).initial_state).toEqual({});
  await page.reload();
  await expandInitialization(page);
  await expect(page.getByRole('button', { name: '添加变量', exact: true })).toBeVisible();
  await expect(page.getByLabel(/^初始化变量名 \d+$/)).toHaveCount(0);
  await addInitializationVariable(page, 'optionalText', '');
  expect((await save(page, datasetId)).initial_state).toEqual({
    config_variables: [{ name: 'optionalText', value: '', type: 'str' }],
  });
});

test('workflow initialization validates names and JSON values, and clears without losing other state', async ({ page }) => {
  const datasetId = await open(page, 'state');
  await addInitializationVariable(page, '', '810000');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor [role=alert]')).toContainText('初始化变量');
  await page.getByLabel('初始化变量名 1', { exact: true }).fill('centreId');
  await addInitializationVariable(page, 'centreId', 'false');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor [role=alert]')).toContainText('不能重复');
  await page.getByRole('button', { name: '删除初始化变量 2', exact: true }).click();
  await save(page, datasetId);
  const original = sample('state');
  const seeded = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, initial_state: { ...original.initial_state, config_variables: [
      { name: 'centreId', value: '810000' }, { name: 'settings', value: { enabled: false } },
    ] } },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.reload();
  await expandInitialization(page);
  await page.getByLabel('初始化变量值 2', { exact: true }).fill('{invalid');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor [role=alert]')).toContainText('JSON');
  await page.getByRole('button', { name: '删除初始化变量 2', exact: true }).click();
  expect((await save(page, datasetId)).initial_state).toEqual({ ticket: 'existing', attempts: 0,
    config_variables: [{ name: 'centreId', value: '810000' }] });
  await page.getByRole('button', { name: '删除初始化变量 1', exact: true }).click();
  expect((await save(page, datasetId)).initial_state).toEqual(sample('state').initial_state);
  await page.reload();
  await expandInitialization(page);
  await expect(page.getByRole('button', { name: '添加变量', exact: true })).toBeVisible();
  await expect(page.getByLabel(/^初始化变量名 \d+$/)).toHaveCount(0);
});

test('workflow initialization keeps text and JSON value types after save and reload', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const textVariables = [
    { name: 'centreId', value: '810000' },
    { name: 'textFalse', value: 'false' },
    { name: 'textObject', value: '{"enabled":false}' },
    { name: 'emptyText', value: '' },
  ];
  const existingVariables = [
    { name: 'count', value: 0 },
    { name: 'enabled', value: false },
    { name: 'optional', value: null },
    { name: 'items', value: [0, false, null, '810000'] },
    { name: 'settings', value: { nested: { active: false, count: 0 } } },
  ];
  const original = sample('state');
  const seeded = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, initial_state: { ...original.initial_state, config_variables: existingVariables } },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.reload();
  await expandInitialization(page);
  for (const index of existingVariables.keys()) {
    await expect(page.getByLabel(`初始化变量类型 ${index + 1}`, { exact: true })).toHaveText('—');
    await expect(page.getByLabel(`初始化变量是否必填 ${index + 1}`, { exact: true })).toHaveText('-');
  }
  for (const variable of textVariables) {
    await addInitializationVariable(page, variable.name, variable.value);
  }
  await expect(page.locator('details.case-initialization select')).toHaveCount(0);
  const variables = [...existingVariables, ...textVariables.map(variable => ({ ...variable, type: 'str' }))];
  expect((await save(page, datasetId)).initial_state.config_variables).toEqual(variables);
  await page.reload();
  await expandInitialization(page);
  for (const [index, variable] of variables.entries()) {
    await expect(page.getByLabel(`初始化变量名 ${index + 1}`, { exact: true })).toHaveValue(variable.name);
    const value = page.getByLabel(`初始化变量值 ${index + 1}`, { exact: true });
    if (typeof variable.value === 'string') await expect(value).toHaveValue(variable.value);
    else await expect.poll(async () => JSON.parse(await value.inputValue())).toEqual(variable.value);
  }
  await page.getByLabel('样本名称', { exact: true }).fill('Re-saved typed configuration');
  expect((await save(page, datasetId)).initial_state.config_variables).toEqual(variables);
});

test('saved base initialization is editable without selecting an agent and retains historical input variables and tool state', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const initial = { ...original.initial_state,
    prompt_variables: [{ name: 'prompt', value: 'retained', type: 'str' },
      { name: 'user_input', value: 'historical input', type: 'str' },
      { name: 'BOT_USER_INPUT', value: 'historical bot input', type: 'str' }],
    tool_variables: [{ name: 'tool', value: { enabled: false }, type: 'custom' }] };
  const turns = [...original.turns,
    { id: 'base-second-turn', input: { user_input: 'second turn', form: { approved: false } }, expectations: [] }];
  const seeded = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, initial_state: initial, turns },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.reload();
  await expandInitialization(page);
  await expect(page.getByRole('group', { name: '初始化类型', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '工作流', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '基础编排', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '自动获取变量', exact: true })).toBeDisabled();
  await expect(page.getByLabel(/^初始化变量名 \d+$/)).toHaveCount(3);
  await expect(page.getByLabel('初始化变量名 1', { exact: true })).toHaveValue('prompt');
  await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('retained');
  await expect(page.getByLabel('初始化变量名 2', { exact: true })).toHaveValue('user_input');
  await expect(page.getByLabel('初始化变量名 3', { exact: true })).toHaveValue('BOT_USER_INPUT');
  await page.getByLabel('初始化变量值 1', { exact: true }).fill('edited prompt');
  await addInitializationVariable(page, 'centreId', '810000');
  const saved = await save(page, datasetId);
  const expected = { ...initial, prompt_variables: [
    { ...initial.prompt_variables[0], value: 'edited prompt' }, ...initial.prompt_variables.slice(1),
    { name: 'centreId', value: '810000', type: 'str' },
  ] };
  expect(saved.initial_state).toEqual(expected);
  expect(saved.turns.map((turn: any) => turn.input)).toEqual(turns.map(turn => turn.input));
  await page.reload();
  await expandInitialization(page);
  await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('edited prompt');
  await expect(page.getByLabel('初始化变量值 2', { exact: true })).toHaveValue('historical input');
  await page.getByLabel('样本名称', { exact: true }).fill('Re-saved base initialization');
  expect((await save(page, datasetId)).initial_state).toEqual(expected);
});

test('malformed workflow variables stay editable and block saving until repaired', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const seeded = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, initial_state: { ...original.initial_state,
      config_variables: 'invalid configuration', prompt_variables: [] } },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.reload();
  await expandInitialization(page);
  const raw = page.getByRole('textbox', { name: '初始化变量原始 JSON', exact: true });
  await expect(raw).toBeVisible();
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor [role=alert]')).toContainText('初始化变量必须为 JSON 数组');
  await raw.fill('[{"name":"centreId","value":"810000"}]');
  expect((await save(page, datasetId)).initial_state).toEqual({ ...original.initial_state,
    config_variables: [{ name: 'centreId', value: '810000' }], prompt_variables: [] });
});

test('manual and saved workflow input variables retain their values and turn inputs', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const variables = [
    { name: 'BOT_USER_INPUT', value: 'saved input', type: 'str', required: true },
    { name: 'attachment', value: 'customer-document.pdf', type: 'doc', required: false },
  ];
  const seeded = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, initial_state: { ...original.initial_state, config_variables: variables } },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.reload();
  await expandInitialization(page);
  const names = page.getByLabel(/^初始化变量名 \d+$/);
  await expect(names).toHaveCount(2);
  await expect(names.nth(0)).toHaveValue('BOT_USER_INPUT');
  await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('saved input');
  await addInitializationVariable(page, 'user_input', 'manual input');
  const saved = await save(page, datasetId);
  expect(saved.initial_state).toEqual({ ...original.initial_state, config_variables: [
    ...variables, { name: 'user_input', value: 'manual input', type: 'str' },
  ] });
  expect(saved.turns.map((turn: any) => turn.input)).toEqual(original.turns.map(turn => turn.input));
  await page.reload();
  await expandInitialization(page);
  await expect(names).toHaveCount(3);
  await expect(names.nth(0)).toHaveValue('BOT_USER_INPUT');
  await expect(names.nth(2)).toHaveValue('user_input');
  await expect(page.getByLabel('初始化变量值 3', { exact: true })).toHaveValue('manual input');
});

async function chooseVariableAgent(page: Page | Locator, label: string, option: string) {
  await page.getByRole('combobox', { name: label, exact: true }).press('Enter');
  await page.getByRole('option', { name: option, exact: true }).click();
}

const variableVersion = '2026-10-16 10:56:13';
const configPath = '/web/agent/getAgentConfigJsonObjectByAgentIdAndVersion';
const workflowVariables = [
  { name: 'userId', type: 'str', required: true },
  { name: 'image', type: 'image', required: true },
  { name: 'doc', type: 'doc', required: false },
  { name: 'audio', type: 'audio', required: false },
  { name: 'BOT_USER_INPUT', type: 'str', required: true },
  { name: 'user_input', type: 'str', required: true },
];
function workflowConfig(variables = workflowVariables) {
  return { code: '0', message: 'success', data: {
    agent_id: 'workflow-test', agent_type: 'workflow_agent', agent_version: '1',
    agent_config: { nodes: [
      { type: 'End', config: { variables: [{ name: 'ignore', type: 'str' }] } },
      { type: 'Start', config: { variables } },
    ] },
  } };
}
const baseVariables = [
  { key: 'userId', type: 'string', required: true },
  { key: 'workspace', type: 'string', required: false,
    default: 'do-not-initialize-from-default', scope: 'session', readonly: true },
  { key: 'user_input', type: 'string', required: true },
  { key: 'BOT_USER_INPUT', type: 'string', required: true },
];
function baseConfig() {
  return { code: '0', message: 'success', data: {
    agent_id: 'base-test', agent_type: 'tool_agent', agent_version: '1',
    agent_config: { prompt_variables: baseVariables },
  } };
}
async function mockVariableDirectory(page: Page, config: (route: Route) => Promise<void>) {
  const calls: { origin: string; path: string; token: string | undefined;
    team: string | null; agent: string | null; version: string | null }[] = [];
  await page.route('**/api/local-targets', route => route.fulfill({ json: { targets: [], unavailable: [] } }));
  await page.route('**/web/**', async route => {
    const url = new URL(route.request().url());
    calls.push({ origin: url.origin, path: url.pathname,
      token: route.request().headers().authorization, team: url.searchParams.get('teamId'),
      agent: url.searchParams.get('agentId'), version: url.searchParams.get('agentVersion') });
    if (url.pathname === '/web/agent/agents') {
      await route.fulfill({ json: { records: [
        { id: 'workflow-test', name: '测试工作流', agentType: 'workflow' },
        { id: 'base-test', name: '测试基础编排', agentType: 'base' },
        { id: 'claw-test', name: '测试分支智能体', agentType: 'abcclaw' },
      ], total: 3, size: 1000, current: 1, pages: 1 } });
    } else if (url.pathname === '/web/agent/getAgentVersionList') {
      await route.fulfill({ json: { code: '0', message: '', data: [
        { agentVersion: variableVersion, updatedAt: variableVersion },
        { agentVersion: 'v2', updatedAt: 'v2' },
      ] } });
    } else if (url.pathname === '/web/abcclaw/v2/branchTree') {
      await route.fulfill({ json: { code: '0', message: '', data: [{ branchId: 'main', branchName: '主分支', children: [] }] } });
    } else if (url.pathname === '/web/abcclaw/v2/listVersions') {
      await route.fulfill({ json: { code: '0', message: '', data: [{ branchId: 'main', agentVersion: 'v2' }] } });
    } else if (url.pathname === configPath) {
      await config(route);
    } else {
      await route.abort();
    }
  });
  return calls;
}
async function selectVariableWorkflow(page: Page | Locator) {
  await expandInitialization(page);
  await chooseVariableAgent(page, '选择智能体', '测试工作流');
  await chooseVariableAgent(page, '智能体版本', variableVersion);
}
async function selectVariableBase(page: Page | Locator) {
  await expandInitialization(page);
  await chooseVariableAgent(page, '选择智能体', '测试基础编排');
  await chooseVariableAgent(page, '智能体版本', variableVersion);
}

for (const mode of ['external', 'bank', 'integrated'] as const) {
  test(`${mode} fetches base variables with matching credentials and saves prompt values without forwarding required metadata`, async ({ page }) => {
    const calls = await mockVariableDirectory(page, route => route.fulfill({ json: baseConfig() }));
    const datasetId = await open(page, 'state');
    if (mode !== 'external') await page.getByRole('button', { name: `Switch to ${mode}`, exact: true }).click();
    await selectVariableBase(page);
    await expect(page.getByRole('group', { name: '初始化类型', exact: true })).toHaveCount(0);
    await addInitializationVariable(page, 'userId', 'kept-user');
    await page.getByRole('button', { name: '自动获取变量', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: '已获取 2 个初始化变量' })).toBeVisible();
    expect(await page.getByLabel(/^初始化变量名 \d+$/).evaluateAll(inputs =>
      inputs.map(input => (input as HTMLInputElement).value))).toEqual(['userId', 'workspace']);
    await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('kept-user');
    await expect(page.getByLabel('初始化变量类型 1', { exact: true })).toHaveText('str');
    await expect(page.getByLabel('初始化变量是否必填 1', { exact: true })).toHaveText('是');
    await expect(page.getByLabel('初始化变量是否必填 2', { exact: true })).toHaveText('否');
    await expect(page.getByLabel('初始化变量值 2', { exact: true })).toHaveValue('');
    await expect(page.getByLabel('初始化变量值 2', { exact: true })).toBeEnabled();
    await page.getByLabel('初始化变量值 2', { exact: true }).fill('customer-workspace');
    const saved = await save(page, datasetId);
    const expected = { ...sample('state').initial_state, prompt_variables: [
      { name: 'userId', value: 'kept-user', type: 'str' },
      { name: 'workspace', value: 'customer-workspace', type: 'str' },
    ] };
    expect(saved.initial_state).toEqual(expected);
    expect(saved.turns.map((turn: any) => turn.input)).toEqual(sample('state').turns.map(turn => turn.input));
    expect(calls.map(call => call.path)).toEqual(['/web/agent/agents', '/web/agent/getAgentVersionList', configPath]);
    expect(calls.filter(call => call.path === configPath)).toMatchObject([
      { agent: 'base-test', version: variableVersion },
    ]);
    expect(calls.every(call => call.origin === (mode === 'external' ? 'http://127.0.0.1:8119' : 'http://bank.invalid'))).toBeTruthy();
    const token = { external: 'local', bank: 'sample-test-token', integrated: 'sample-integrated-token' }[mode];
    expect(calls.every(call => call.token === `Bearer ${token}`)).toBeTruthy();
    expect(calls[0].team).toBe({ external: null, bank: 'team-test', integrated: 'integrated-team' }[mode]);
    await page.reload();
    await expandInitialization(page);
    await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('kept-user');
    await expect(page.getByLabel('初始化变量值 2', { exact: true })).toHaveValue('customer-workspace');
    await expect(page.getByLabel('初始化变量是否必填 1', { exact: true })).toHaveText('-');
    await page.getByLabel('样本名称', { exact: true }).fill('Saved base variables');
    expect((await save(page, datasetId)).initial_state).toEqual(expected);
  });
}

test('an empty sample can manually save base initialization after selecting its agent without fetching', async ({ page }) => {
  const calls = await mockVariableDirectory(page, route => route.fulfill({ json: baseConfig() }));
  const datasetId = await open(page);
  await selectVariableBase(page);
  await addInitializationVariable(page, 'user_input', 'manual initialization input');
  await page.getByLabel('用户输入', { exact: true }).fill('independent turn input');
  const saved = await save(page, datasetId, 'POST');
  expect(saved.initial_state).toEqual({ prompt_variables: [
    { name: 'user_input', value: 'manual initialization input', type: 'str' },
  ] });
  expect(saved.turns[0].input).toEqual({ txt: 'independent turn input' });
  expect(calls.filter(call => call.path === configPath)).toEqual([]);
});

test('successful base and workflow fetches replace the initialization format while retaining shared values and unrelated data', async ({ page }) => {
  await mockVariableDirectory(page, route => route.fulfill({ json:
    new URL(route.request().url()).searchParams.get('agentId') === 'base-test'
      ? baseConfig() : workflowConfig([{ name: 'userId', type: 'str', required: true }]),
  }));
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const initial = { ...original.initial_state,
    config_variables: [{ name: 'userId', value: 'shared-user', type: 'str', required: true },
      { name: 'workflowOnly', value: 'previous workflow value', type: 'str' }],
    tool_variables: [{ name: 'lookup', value: { enabled: false }, type: 'custom' }],
  };
  const turns = [...original.turns,
    { id: 'format-second-turn', input: { user_input: 'second turn', menuId: 'ACCOUNT' }, expectations: [] }];
  expect((await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, initial_state: initial, turns },
  })).ok()).toBeTruthy();
  await page.reload();
  await selectVariableBase(page);
  await expect(page.getByLabel('初始化变量名 2', { exact: true })).toHaveValue('workflowOnly');
  const fetchButton = page.getByRole('button', { name: '自动获取变量', exact: true });
  await fetchButton.click();
  await expect(page.getByRole('status').filter({ hasText: '已获取 2 个初始化变量' })).toBeVisible();
  await page.getByLabel('初始化变量值 2', { exact: true }).fill('base-workspace');
  const baseSaved = await save(page, datasetId);
  expect(baseSaved.initial_state).toEqual({ ...original.initial_state, tool_variables: initial.tool_variables,
    prompt_variables: [{ name: 'userId', value: 'shared-user', type: 'str' },
      { name: 'workspace', value: 'base-workspace', type: 'str' }] });
  expect(baseSaved.turns.map((turn: any) => turn.input)).toEqual(turns.map(turn => turn.input));
  await selectVariableWorkflow(page);
  await expect(page.getByLabel('初始化变量名 2', { exact: true })).toHaveValue('workspace');
  await fetchButton.click();
  await expect(page.getByRole('status').filter({ hasText: '已获取 1 个初始化变量' })).toBeVisible();
  const workflowSaved = await save(page, datasetId);
  expect(workflowSaved.initial_state).toEqual({ ...original.initial_state, tool_variables: initial.tool_variables,
    config_variables: [{ name: 'userId', value: 'shared-user', type: 'str', required: true }] });
  expect(workflowSaved.turns.map((turn: any) => turn.input)).toEqual(turns.map(turn => turn.input));
});

for (const target of ['base', 'workflow'] as const) {
  test(`failed ${target} fetch retains the saved initialization format and values`, async ({ page }) => {
    await mockVariableDirectory(page, route => route.fulfill({ status: 503, json: {} }));
    const datasetId = await open(page, 'state');
    const original = sample('state');
    const field = target === 'base' ? 'config_variables' : 'prompt_variables';
    const initial = { ...original.initial_state,
      [field]: [{ name: 'userId', value: 'saved-user', type: 'str' }],
      tool_variables: [{ name: 'tool', value: [0, false], type: 'custom' }],
    };
    expect((await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
      data: { ...original, initial_state: initial },
    })).ok()).toBeTruthy();
    await page.reload();
    await (target === 'base' ? selectVariableBase(page) : selectVariableWorkflow(page));
    await page.getByLabel('初始化变量值 1', { exact: true }).fill('retained-edit');
    await page.getByRole('button', { name: '自动获取变量', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: '获取变量失败' })).toBeVisible();
    await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('retained-edit');
    const saved = await save(page, datasetId);
    expect(saved.initial_state).toEqual({ ...initial,
      [field]: [{ name: 'userId', value: 'retained-edit', type: 'str' }] });
    expect(saved.turns[0].input).toEqual(original.turns[0].input);
  });
}

test('a late base response cannot replace workflow variables after selecting another agent', async ({ page }) => {
  let release!: () => void;
  let arrived!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { arrived = resolve; });
  await mockVariableDirectory(page, async route => {
    arrived();
    await pending;
    await route.fulfill({ json: baseConfig() });
  });
  const datasetId = await open(page, 'state');
  await addInitializationVariable(page, 'userId', 'workflow-value');
  await selectVariableBase(page);
  await page.getByRole('button', { name: '自动获取变量', exact: true }).click();
  await requested;
  await selectVariableWorkflow(page);
  const response = page.waitForResponse(response => new URL(response.url()).pathname === configPath);
  release();
  await response;
  await expect(page.getByRole('status').filter({ hasText: '已获取' })).toHaveCount(0);
  await expect(page.getByLabel(/^初始化变量名 \d+$/)).toHaveCount(1);
  expect((await save(page, datasetId)).initial_state).toEqual({ ...sample('state').initial_state,
    config_variables: [{ name: 'userId', value: 'workflow-value', type: 'str' }] });
});

for (const mode of ['external', 'bank'] as const) {
  test(`${mode} fetches workflow variables with matching credentials and preserves file variable values through refresh and publication`, async ({ page }) => {
    const calls = await mockVariableDirectory(page, route => route.fulfill({ json: workflowConfig() }));
    const datasetId = await open(page, 'state');
    if (mode === 'bank') await page.getByRole('button', { name: 'Switch to bank' }).click();
    await expandInitialization(page);
    const fetchButton = page.getByRole('button', { name: '自动获取变量', exact: true });
    await expect(fetchButton).toBeDisabled();
    await chooseVariableAgent(page, '选择智能体', '测试工作流');
    await expect(page.getByRole('combobox', { name: '分支地址', exact: true })).toBeDisabled();
    await expect(fetchButton).toBeDisabled();
    await chooseVariableAgent(page, '智能体版本', variableVersion);
    await expect(fetchButton).toBeEnabled();
    await expect(page.getByTestId('dirty')).toHaveText('false');
    await addInitializationVariable(page, 'userId', 'manual-value');
    await expect(page.locator('.variable-columns')).toContainText('类型');
    await expect(page.locator('.variable-columns')).toContainText('是否必填');
    await expect(page.getByLabel('初始化变量类型 1', { exact: true })).toHaveText('str');
    await expect(page.getByLabel('初始化变量是否必填 1', { exact: true })).toHaveText('-');
    await fetchButton.click();
    await expect(page.getByRole('status').filter({ hasText: '已获取 4 个初始化变量' })).toBeVisible();
    await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('manual-value');
    await expect(page.getByLabel(/^初始化变量名 \d+$/)).toHaveCount(4);
    expect(await page.getByLabel(/^初始化变量名 \d+$/).evaluateAll(inputs =>
      inputs.map(input => (input as HTMLInputElement).value))).toEqual(['userId', 'image', 'doc', 'audio']);
    await expect(page.getByLabel('初始化变量类型 1', { exact: true })).toHaveText('str');
    await expect(page.getByLabel('初始化变量是否必填 1', { exact: true })).toHaveText('是');
    await expect(page.getByLabel('初始化变量类型 3', { exact: true })).toHaveText('doc');
    await expect(page.getByLabel('初始化变量是否必填 3', { exact: true })).toHaveText('否');
    const fileValues: Record<string, string> = {
      image: 'customer-image.png',
      doc: 'customer-document.pdf',
      audio: 'customer-audio.wav',
    };
    for (const [name, value] of Object.entries(fileValues)) {
      const index = workflowVariables.findIndex(variable => variable.name === name) + 1;
      const input = page.getByLabel(`初始化变量值 ${index}`, { exact: true });
      await expect(input).toBeEnabled();
      await expect(input).toHaveValue('');
      await expect(input).toHaveAttribute('placeholder', '填写变量值');
      await input.fill(value);
    }
    expect(calls.filter(call => call.path === configPath)).toMatchObject([
      { agent: 'workflow-test', version: variableVersion },
    ]);
    const refreshed = page.waitForResponse(response => new URL(response.url()).pathname === configPath);
    await fetchButton.click();
    expect((await refreshed).ok()).toBeTruthy();
    await expect(fetchButton).toBeEnabled();
    for (const [name, value] of Object.entries(fileValues)) {
      const index = workflowVariables.findIndex(variable => variable.name === name) + 1;
      await expect(page.getByLabel(`初始化变量值 ${index}`, { exact: true })).toHaveValue(value);
      await expect(page.getByLabel(`初始化变量类型 ${index}`, { exact: true })).toHaveText(name);
    }
    const expectedVariables = workflowVariables.slice(0, 4).map(variable => ({ ...variable,
      value: variable.name === 'userId' ? 'manual-value' : (fileValues[variable.name] ?? ''),
    }));
    expect((await save(page, datasetId)).initial_state).toEqual({ ...sample('state').initial_state,
      config_variables: expectedVariables });
    await chooseVariableAgent(page, '选择智能体', '测试分支智能体');
    await expect(fetchButton).toBeDisabled();
    await expect(page.getByRole('status').filter({ hasText: '已获取' })).toHaveCount(0);
    await chooseVariableAgent(page, '分支地址', '主分支 · main');
    await chooseVariableAgent(page, '智能体版本', 'v2');
    await expect(fetchButton).toBeDisabled();
    expect(calls.map(call => call.path)).toEqual([
      '/web/agent/agents', '/web/agent/getAgentVersionList', configPath, configPath, '/web/agent/agents',
      '/web/abcclaw/v2/branchTree', '/web/abcclaw/v2/listVersions',
    ]);
    expect(calls.every(call => call.origin === (mode === 'bank' ? 'http://bank.invalid' : 'http://127.0.0.1:8119'))).toBeTruthy();
    expect(calls.every(call => call.token === (mode === 'bank' ? 'Bearer sample-test-token' : 'Bearer local'))).toBeTruthy();
    expect(calls[0].team).toBe(mode === 'bank' ? 'team-test' : null);
    await page.getByRole('button', { name: mode === 'bank' ? 'Switch to external' : 'Switch to bank' }).click();
    await expect(fetchButton).toBeDisabled();
    await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('manual-value');
    expect((await save(page, datasetId)).initial_state).toEqual({ ...sample('state').initial_state,
      config_variables: expectedVariables });
    await page.reload();
    await expandInitialization(page);
    await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('manual-value');
    for (const [name, value] of Object.entries(fileValues)) {
      const index = workflowVariables.findIndex(variable => variable.name === name) + 1;
      await expect(page.getByLabel(`初始化变量值 ${index}`, { exact: true })).toBeEnabled();
      await expect(page.getByLabel(`初始化变量值 ${index}`, { exact: true })).toHaveValue(value);
    }
    const published = page.waitForResponse(response => response.url().endsWith('/drafts/publish'));
    await page.getByRole('button', { name: 'Publish acceptance draft' }).click();
    expect((await published).ok()).toBeTruthy();
    for (const [name, value] of Object.entries(fileValues)) {
      const index = workflowVariables.findIndex(variable => variable.name === name) + 1;
      await expect(page.getByLabel(`初始化变量值 ${index}`, { exact: true })).toBeDisabled();
      await expect(page.getByLabel(`初始化变量值 ${index}`, { exact: true })).toHaveValue(value);
    }
    const publishedVersion = await page.request.get(baseUrl + `/api/datasets/${datasetId}/versions/1`);
    expect(publishedVersion.ok()).toBeTruthy();
    expect((await publishedVersion.json()).data.cases[0].initial_state.config_variables).toEqual(expectedVariables);
  });
}

test('workflow variable fetch blocks editing-page save until the returned variables can be persisted', async ({ page }) => {
  let release!: () => void;
  let arrived!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { arrived = resolve; });
  const variables = [{ name: 'userId', type: 'str', required: true }];
  await mockVariableDirectory(page, async route => {
    arrived();
    await pending;
    await route.fulfill({ json: workflowConfig(variables) });
  });
  const datasetId = await open(page, 'state');
  const mutations: { initial_state: unknown }[] = [];
  page.on('request', request => {
    if (request.method() === 'PUT' &&
        new URL(request.url()).pathname === `/api/datasets/${datasetId}/drafts/cases/${sample('state').id}`) {
      mutations.push(request.postDataJSON());
    }
  });
  await selectVariableWorkflow(page);
  await page.getByRole('button', { name: '自动获取变量', exact: true }).click();
  await requested;
  const saveButton = page.getByRole('button', { name: '保存样本', exact: true });
  await expect(saveButton).toBeDisabled();
  await saveButton.dispatchEvent('click');
  expect(mutations).toEqual([]);
  release();
  await expect(page.getByRole('status').filter({ hasText: '已获取 1 个初始化变量' })).toBeVisible();
  await expect(saveButton).toBeEnabled();
  await page.getByLabel('初始化变量值 1', { exact: true }).fill('fetched-user');
  const expectedInitial = { ...sample('state').initial_state,
    config_variables: [{ ...variables[0], value: 'fetched-user' }] };
  expect((await save(page, datasetId)).initial_state).toEqual(expectedInitial);
  expect(mutations).toHaveLength(1);
  expect(mutations[0].initial_state).toEqual(expectedInitial);
  await expandInitialization(page);
  await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('fetched-user');
});

test('workflow variable fetch stays disabled while the existing sample save is pending', async ({ page }) => {
  const calls = await mockVariableDirectory(page, route => route.fulfill({ json: workflowConfig() }));
  const datasetId = await open(page, 'state');
  await selectVariableWorkflow(page);
  await page.getByLabel('样本名称', { exact: true }).fill('Saving before fetching');
  let release!: () => void;
  let arrived!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { arrived = resolve; });
  const casePath = `/api/datasets/${datasetId}/drafts/cases/${sample('state').id}`;
  await page.route(`**${casePath}`, async route => {
    if (route.request().method() !== 'PUT') return route.continue();
    arrived();
    await pending;
    await route.continue();
  });
  const savedResponse = page.waitForResponse(response => response.request().method() === 'PUT' &&
    new URL(response.url()).pathname === casePath);
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await requested;
  const fetchButton = page.getByRole('button', { name: '自动获取变量', exact: true });
  await expect(fetchButton).toBeDisabled();
  await fetchButton.dispatchEvent('click');
  expect(calls.filter(call => call.path === configPath)).toEqual([]);
  release();
  expect((await savedResponse).ok()).toBeTruthy();
  await expect(page.getByTestId('dirty')).toHaveText('false');
  await expect(fetchButton).toBeEnabled();
  expect(calls.filter(call => call.path === configPath)).toEqual([]);
  await fetchButton.click();
  await expect(page.getByRole('status').filter({ hasText: '已获取 4 个初始化变量' })).toBeVisible();
  expect(calls.filter(call => call.path === configPath)).toHaveLength(1);
});

test('manual wizard footer waits for workflow variables and creates the sample with them', async ({ page }) => {
  let release!: () => void;
  let arrived!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { arrived = resolve; });
  const variables = [{ name: 'userId', type: 'str', required: true }];
  await mockVariableDirectory(page, async route => {
    arrived();
    await pending;
    await route.fulfill({ json: workflowConfig(variables) });
  });
  const datasetId = await open(page);
  const casePath = `/api/datasets/${datasetId}/drafts/cases`;
  const mutations: { initial_state: unknown }[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === casePath) {
      mutations.push(request.postDataJSON());
    }
  });
  await page.getByRole('button', { name: 'Open manual sample wizard', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: '下一步', exact: true }).click();
  await dialog.getByLabel('样本名称', { exact: true }).fill('Workflow variables from wizard');
  await dialog.getByLabel('用户输入', { exact: true }).fill('Create after variables arrive');
  await selectVariableWorkflow(dialog);
  await expect(dialog.locator('.sample-editor').getByRole('button', { name: '保存样本', exact: true })).toHaveCount(0);
  const saveButton = dialog.getByRole('button', { name: '保存样本', exact: true });
  await expect(saveButton).toBeEnabled();
  await dialog.getByRole('button', { name: '自动获取变量', exact: true }).click();
  await requested;
  await expect(saveButton).toBeDisabled();
  await saveButton.dispatchEvent('click');
  expect(mutations).toEqual([]);
  release();
  await expect(dialog.getByRole('status').filter({ hasText: '已获取 1 个初始化变量' })).toBeVisible();
  await expect(saveButton).toBeEnabled();
  await dialog.getByLabel('初始化变量值 1', { exact: true }).fill('wizard-user');
  const response = page.waitForResponse(response => response.request().method() === 'POST' &&
    new URL(response.url()).pathname === casePath);
  await saveButton.click();
  expect((await response).ok()).toBeTruthy();
  await expect(dialog.getByText('创建完成', { exact: false })).toBeVisible();
  const expectedInitial = { config_variables: [{ ...variables[0], value: 'wizard-user' }] };
  expect(mutations).toHaveLength(1);
  expect(mutations[0].initial_state).toEqual(expectedInitial);
  const draft = await page.request.get(baseUrl + `/api/datasets/${datasetId}/drafts/current`);
  expect(draft.ok()).toBeTruthy();
  const saved = (await draft.json()).data.cases;
  expect(saved).toHaveLength(1);
  expect(saved[0].initial_state).toEqual(expectedInitial);
  expect(saved[0].turns[0].input).toEqual({ txt: 'Create after variables arrive' });
});

for (const failure of ['http', 'business', 'json', 'network'] as const) {
  test(`workflow variable ${failure} error retains edits and permits retry`, async ({ page }) => {
    let failed = true;
    await mockVariableDirectory(page, async route => {
      if (!failed) return route.fulfill({ json: workflowConfig() });
      if (failure === 'http') return route.fulfill({ status: 401, json: {} });
      if (failure === 'business') return route.fulfill({ json: { code: '1', message: 'failure', data: null } });
      if (failure === 'json') return route.fulfill({ contentType: 'application/json', body: '{' });
      return route.abort();
    });
    const datasetId = await open(page, 'state');
    await selectVariableWorkflow(page);
    await addInitializationVariable(page, 'userId', 'keep-me');
    const fetchButton = page.getByRole('button', { name: '自动获取变量', exact: true });
    await fetchButton.click();
    await expect(page.getByRole('status').filter({ hasText: '获取变量失败' })).toBeVisible();
    await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('keep-me');
    await expect(fetchButton).toBeEnabled();
    expect((await save(page, datasetId)).initial_state.config_variables).toEqual([{ name: 'userId', value: 'keep-me', type: 'str' }]);
    failed = false;
    await fetchButton.click();
    await expect(page.getByRole('status').filter({ hasText: '已获取 4 个' })).toBeVisible();
    await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('keep-me');
  });
}

test('workflow without variables can be saved without adding initialization configuration', async ({ page }) => {
  await mockVariableDirectory(page, route => route.fulfill({ json: workflowConfig([]) }));
  const datasetId = await open(page, 'state');
  await selectVariableWorkflow(page);
  await page.getByRole('button', { name: '自动获取变量', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: '没有初始化变量' })).toBeVisible();
  expect((await save(page, datasetId)).initial_state).toEqual(sample('state').initial_state);
});

for (const change of ['version', 'login', 'sample'] as const) {
  test(`late variable response cannot populate a different ${change}`, async ({ page }) => {
    let release!: () => void;
    let arrived!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    const requested = new Promise<void>(resolve => { arrived = resolve; });
    const calls = await mockVariableDirectory(page, async route => {
      arrived();
      await pending;
      await route.fulfill({ json: workflowConfig() });
    });
    await open(page, 'state');
    await selectVariableWorkflow(page);
    await addInitializationVariable(page, 'userId', 'keep-me');
    const fetchButton = page.getByRole('button', { name: '自动获取变量', exact: true });
    await fetchButton.click();
    await requested;
    await expect(page.getByRole('button', { name: '获取中…', exact: true })).toBeDisabled();
    if (change === 'version') await chooseVariableAgent(page, '智能体版本', 'v2');
    if (change === 'login') await page.getByRole('button', { name: 'Switch to bank' }).click();
    if (change === 'sample') await page.getByRole('button', { name: 'Reload server sample' }).click();
    if (change === 'sample') await expect(page.getByLabel('初始化变量名 1', { exact: true })).toHaveCount(0);
    else await expect(fetchButton).toBeVisible();
    const response = page.waitForResponse(response => new URL(response.url()).pathname === configPath);
    release();
    await response;
    await expect(page.getByRole('status').filter({ hasText: '已获取' })).toHaveCount(0);
    await expect(page.getByLabel('初始化变量名 2', { exact: true })).toHaveCount(0);
    if (change !== 'sample') await expect(page.getByLabel('初始化变量值 1', { exact: true })).toHaveValue('keep-me');
    expect(calls.filter(call => call.path === configPath)).toHaveLength(1);
  });
}

async function expectAnnotationInputsAbsent(page: Page) {
  await expect(page.getByLabel('背景知识', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('自定义变量', { exact: true })).toHaveCount(0);
}

test('hidden annotations survive editing and reloading without changing other turn data', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const extensions = [
    { key: 'custom', value: 'keep' },
    { key: 'background', value: '客户已完成身份核验\n仅供记录' },
    { key: 'custom_variables', value: '{"cust_id":"001","amount":100}\n备注=保持原文' },
  ];
  const seeded = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, turns: original.turns.map(turn => ({
      ...turn, extensions,
    })) },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByLabel('样本名称', { exact: true })).toHaveValue(original.name);
  await expectAnnotationInputsAbsent(page);
  await page.getByLabel('样本名称', { exact: true }).fill('Edited sample with hidden annotations');
  await expect(page.getByTestId('dirty')).toHaveText('true');
  const saved = await save(page, datasetId);
  expect(saved.turns[0].extensions).toEqual(extensions);
  expect(saved.turns[0].input).toEqual(original.turns[0].input);
  expect(saved.turns[0].expectations).toEqual(original.turns[0].expectations);
  await page.reload();
  await expect(page.getByLabel('样本名称', { exact: true })).toHaveValue(saved.name);
  await expectAnnotationInputsAbsent(page);
  await page.getByLabel('样本分类', { exact: true }).selectOption('boundary');
  expect((await save(page, datasetId)).turns[0].extensions).toEqual(extensions);
});

test('hidden annotations stay with each turn and survive publication', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const extensions = [
    [{ key: 'background', value: '第一轮背景' }, { key: 'custom_variables', value: 'channel=web' },
      { key: 'custom', value: 'first-only' }],
    [{ key: 'background', value: '第二轮背景' }, { key: 'custom_variables', value: 'channel=mobile' },
      { key: 'custom', value: 'second-only' }],
  ];
  const seeded = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, turns: [
      { ...original.turns[0], extensions: extensions[0] },
      { id: 'annotation-second-turn', input: { query: '第二轮输入' }, expectations: [], extensions: extensions[1] },
    ] },
  });
  expect(seeded.ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByLabel('样本名称', { exact: true })).toHaveValue(original.name);
  await expectAnnotationInputsAbsent(page);
  await page.getByRole('button', { name: /第 2 轮/ }).click();
  await expectAnnotationInputsAbsent(page);
  await page.getByLabel('用户输入', { exact: true }).fill('第二轮修改后的输入');
  await page.getByRole('button', { name: '添加新轮次' }).click();
  await expectAnnotationInputsAbsent(page);
  await page.getByLabel('用户输入', { exact: true }).fill('无历史附加信息的新轮次');
  const saved = await save(page, datasetId);
  expect(saved.turns[0].input).toEqual(original.turns[0].input);
  expect(saved.turns[0].expectations).toEqual(original.turns[0].expectations);
  expect(saved.turns[1].input).toEqual({ query: '第二轮修改后的输入' });
  expect(saved.turns).toHaveLength(3);
  expect(saved.turns.slice(0, 2).map((turn: { extensions: unknown }) => turn.extensions)).toEqual(extensions);
  expect(saved.turns[2]).not.toHaveProperty('extensions');
  await page.reload();
  await expect(page.getByLabel('样本名称', { exact: true })).toHaveValue(original.name);
  await page.getByRole('button', { name: /第 2 轮/ }).click();
  await expectAnnotationInputsAbsent(page);
  const publication = page.waitForResponse(response => response.url().endsWith('/drafts/publish'));
  await page.getByRole('button', { name: 'Publish acceptance draft' }).click();
  expect((await publication).ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByLabel('样本名称', { exact: true })).toBeDisabled();
  await expectAnnotationInputsAbsent(page);
  await page.getByRole('button', { name: /第 2 轮/ }).click();
  await expectAnnotationInputsAbsent(page);
  const response = await page.request.get(baseUrl + `/api/datasets/${datasetId}/versions/1`);
  const published = (await response.json()).data.cases[0];
  expect(published.turns).toHaveLength(3);
  expect(published.turns.slice(0, 2).map((turn: { extensions: unknown }) => turn.extensions)).toEqual(extensions);
  expect(published.turns[2]).not.toHaveProperty('extensions');
  await test.info().attach('annotations-hidden-published', {
    body: await page.screenshot({ fullPage: true }), contentType: 'image/png',
  });
});

test('removed extension controls remain absent while each turn retains its stored data', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const extensions = [
    { key: 'background', value: '第一轮背景' },
    { key: 'custom_variables', value: '{"cust_id":"001"}\n保持原文' },
    { key: 'custom', value: 'keep' },
  ];
  const seeded = {
    ...original,
    turns: [
      { ...original.turns[0], extensions },
      { id: 'second-turn', input: { query: '第二轮输入' }, notes: '', expectations: [],
        extensions: [{ key: 'background', value: '第二轮背景' },
          { key: 'custom_variables', value: 'channel=mobile' }] },
    ],
  };
  expect((await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: seeded,
  })).ok()).toBeTruthy();
  await page.reload();
  for (const label of ['背景知识', '自定义变量'])
    await expect(page.getByLabel(label, { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '对话预览', exact: true })).toHaveCount(0);
  await expect(page.getByText('期望评估方式', { exact: true })).toHaveCount(0);
  await page.getByLabel('样本名称', { exact: true }).fill('Renamed sample');
  await page.getByRole('button', { name: /第 2 轮/ }).click();
  await page.getByLabel('用户输入', { exact: true }).fill('第二轮修改');
  await page.getByRole('button', { name: /第 1 轮/ }).click();
  const saved = await save(page, datasetId);
  expect(saved.turns.map((turn: any) => turn.extensions)).toEqual(seeded.turns.map(turn => turn.extensions));
  expect(saved.turns[0].expectations).toEqual(original.turns[0].expectations);
  expect(saved.turns[1].input).toEqual({ query: '第二轮修改' });
  const publication = page.waitForResponse(response => response.url().endsWith('/drafts/publish'));
  await page.getByRole('button', { name: 'Publish acceptance draft' }).click();
  expect((await publication).ok()).toBeTruthy();
  await page.reload();
  for (const label of ['背景知识', '自定义变量'])
    await expect(page.getByLabel(label, { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('用户输入', { exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '保存样本', exact: true })).toHaveCount(0);
  const published = await page.request.get(baseUrl + `/api/datasets/${datasetId}/versions/1`);
  expect(published.ok()).toBeTruthy();
  expect((await published.json()).data.cases[0].turns.map((turn: any) => turn.extensions))
    .toEqual(seeded.turns.map(turn => turn.extensions));
});

test('editing the primary expected output preserves every visible secondary condition', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const secondary = [
    { id: 'regex-output', name: '保留正则', kind: 'output', path: 'answer',
      condition: { kind: 'matches_pattern', pattern: '^old$' } },
    { id: 'range-output', name: '保留区间', kind: 'output', path: 'score',
      condition: { kind: 'within_range', minimum: 0, maximum: 1 } },
    { id: 'missing-output', name: '保留缺失', kind: 'output', path: 'secret',
      condition: { kind: 'must_be_missing' } },
  ];
  const primary = { id: 'primary-output', name: '完整输出', kind: 'output', path: null,
    condition: { kind: 'equals', expected: 'old answer' } };
  const rules = [...original.turns[0].expectations, ...secondary, primary];
  expect((await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, turns: [{ ...original.turns[0], expectations: rules }] },
  })).ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('old answer');
  await expect(page.locator('.expectation-row')).toHaveCount(5);
  await expect((await namedExpectation(page, '完整输出')).getByTestId(/^expectation-value-/)).toHaveValue('"old answer"');
  await expect(expectationInput(page, 'path', 1)).toHaveValue('answer');
  await expect(expectationRow(page, 1).getByPlaceholder('正则表达式')).toHaveValue('^old$');
  await expect(page.getByText('期望评估方式', { exact: true })).toHaveCount(0);
  await page.getByLabel('期望输出', { exact: true }).fill('new answer');
  const saved = await save(page, datasetId);
  expect(saved.turns[0].expectations).toEqual([
    ...rules.slice(0, -1), { ...primary, condition: { kind: 'equals', expected: 'new answer' } },
  ]);
  await page.reload();
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('new answer');
});

test('user input is required and expected output stays optional', async ({ page }) => {
  const datasetId = await open(page);
  const input = page.getByLabel('用户输入', { exact: true });
  await expect(input).toHaveAttribute('aria-required', 'true');
  await expect(page.locator('.message-card.user header')).toContainText('用户输入 *');
  await expect(page.locator('.message-card.expected header')).toContainText('期望输出');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor [role="alert"]')).toContainText('用户输入不能为空');
  expect((await (await page.request.get(baseUrl + `/api/datasets/${datasetId}/drafts/current`)).json()).data.cases)
    .toHaveLength(0);
  await input.fill('只提供用户输入');
  const saved = await save(page, datasetId, 'POST');
  expect(saved.turns[0].input).toEqual({ txt: '只提供用户输入' });
  expect(saved.turns[0].expectations).toEqual([]);
});

test('classification saves negative, reopens, changes to boundary and becomes read-only when published', async ({ page }) => {
  const datasetId = await open(page);
  const category = page.getByLabel('样本分类', { exact: true });
  await expect(category).toHaveValue('positive');
  await category.selectOption('negative');
  await expect(page.getByTestId('dirty')).toHaveText('true');
  await page.getByLabel('用户输入', { exact: true }).fill('请拒绝此请求');
  expect((await save(page, datasetId, 'POST')).category).toBe('negative');
  await page.reload();
  await expect(category).toHaveValue('negative');
  await category.selectOption('boundary');
  expect((await save(page, datasetId)).category).toBe('boundary');
  await page.reload();
  await expect(category).toHaveValue('boundary');
  const publication = page.waitForResponse(response => response.url().endsWith('/drafts/publish'));
  await page.getByRole('button', { name: 'Publish acceptance draft' }).click();
  expect((await publication).ok()).toBeTruthy();
  await expect(category).toBeDisabled();
  await expect(category).toHaveValue('boundary');
  await expect(page.getByRole('button', { name: '保存样本', exact: true })).toHaveCount(0);
  await page.reload();
  await expect(category).toBeDisabled();
  await expect(category).toHaveValue('boundary');
  await test.info().attach('published-boundary-sample', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
});

for (const kind of ['state', 'tool_argument'] as const) {
  for (const action of ['Backspace', 'Delete', 'clear'] as const) {
    test(`${kind} permits ${action} without reformatting incomplete JSON`, async ({ page }) => {
      const datasetId = await open(page, kind);
      const input = valueInput(page, kind);
      const original = editorText(kind, 'abcd');
      await expect(input).toHaveValue(original);
      if (action === 'clear') {
        await input.press('ControlOrMeta+A');
        await input.press('Backspace');
        await expect(input).toHaveValue('');
      } else {
        await input.press('ControlOrMeta+A');
        await input.press(action === 'Backspace' ? 'ArrowRight' : 'ArrowLeft');
        await input.press(action);
        await expect(input).toHaveValue(action === 'Backspace' ? original.slice(0, -1) : original.slice(1));
      }
      const expected = '拒绝 C:\\path "quoted"';
      const raw = editorText(kind, expected);
      await typeRaw(input, raw);
      const saved = await save(page, datasetId);
      expect(saved.turns[0].expectations.find((rule: any) => rule.id === 'editable-rule'))
        .toEqual({ ...sample(kind).turns[0].expectations[0], condition: { kind: 'equals', expected } });
      await page.reload();
      await expect(valueInput(page, kind)).toHaveValue(raw);
    });
  }

  test(`${kind} persists typed values while retaining field metadata`, async ({ page }) => {
    test.setTimeout(90000);
    const datasetId = await open(page, kind);
    const values = ['quoted string', 'C:\\temp\\file', 0, 12.5, true, false, null,
      [0, true, null, ['x', [false, 2]]]];
    for (const value of values) {
      const raw = editorText(kind, value);
      await typeRaw(valueInput(page, kind), raw);
      await expect(page.getByTestId('dirty')).toHaveText('true');
      const saved = await save(page, datasetId);
      const expected = sample(kind);
      expected.turns[0].expectations[0].condition.expected = value as any;
      expect(saved).toEqual(expected);
      await page.reload();
      await expect(valueInput(page, kind)).toHaveValue(raw);
    }
  });

  test(`${kind} blocks invalid JSON from saving or abandoning the current turn`, async ({ page }) => {
    const datasetId = await open(page, kind);
    const raw = '{"unfinished":';
    await typeRaw(valueInput(page, kind), raw);
    await page.getByLabel('样本名称', { exact: true }).fill('Unsaved name');
    await expect(valueInput(page, kind)).toHaveValue(raw);
    await expect(page.getByTestId('dirty')).toHaveText('true');
    await page.getByRole('button', { name: '保存样本', exact: true }).click();
    await expect(page.locator('.sample-editor > [role="alert"]')).toContainText('JSON');
    await page.getByRole('button', { name: '添加新轮次' }).click();
    await expect(page.locator('.round')).toHaveCount(1);
    await expect(valueInput(page, kind)).toHaveValue(raw);
    const persisted = await page.request.get(baseUrl + `/api/datasets/${datasetId}/drafts/current`);
    expect((await persisted.json()).data.cases[0]).toEqual(sample(kind));
  });

  test(`${kind} is read-only with matching fields after publication`, async ({ page }) => {
    await open(page, kind);
    const publication = page.waitForResponse(response => response.url().endsWith('/drafts/publish'));
    await page.getByRole('button', { name: 'Publish acceptance draft' }).click();
    expect((await publication).ok()).toBeTruthy();
    await page.reload();
    await expect(valueInput(page, kind)).toHaveValue(editorText(kind, 'abcd'));
    await expect(valueInput(page, kind)).toBeDisabled();
    await expect(expectationInput(page, 'path')).toBeDisabled();
    if (kind === 'tool_argument') await expect(expectationInput(page, 'tool')).toBeDisabled();
    await expect(page.getByRole('button', { name: '保存样本', exact: true })).toHaveCount(0);
    await expect(page.getByTestId('add-expectation')).toHaveCount(0);
    await expect(expectationRow(page).getByRole('button', { name: '删除', exact: true })).toHaveCount(0);
    await expect(page.getByText('期望评估方式', { exact: true })).toHaveCount(0);
  });
}

test('saving untouched state and tool rules preserves metadata and all other expectations', async ({ page }) => {
  for (const kind of ['state', 'tool_argument'] as const) {
    const datasetId = await open(page, kind);
    expect(await save(page, datasetId)).toEqual(sample(kind));
    await page.reload();
    await expect(valueInput(page, kind)).toHaveValue(editorText(kind, 'abcd'));
  }
});

test('deleting a final state rule preserves other checks across unrelated edits', async ({ page }) => {
  const datasetId = await open(page, 'state');
  await expectationRow(page).getByRole('button', { name: '删除', exact: true }).click();
  await page.getByLabel('样本名称', { exact: true }).fill('Renamed sample');
  await expect(page.locator('.expectation-row')).toHaveCount(0);
  const saved = await save(page, datasetId);
  expect(saved.name).toBe('Renamed sample');
  expect(saved.turns[0].expectations).toEqual([sample('state').turns[0].expectations[1]]);
  await page.reload();
  await expect(page.locator('.expectation-row')).toHaveCount(0);
  await expect(page.getByText('暂无字段、状态或输出期望')).toBeVisible();
});

test('tool argument rules require a tool and path and can be explicitly removed', async ({ page }) => {
  const datasetId = await open(page, 'tool_argument');
  await expectationInput(page, 'tool').fill('');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor > [role="alert"]')).toContainText('工具名');
  await expectationInput(page, 'tool').fill('lookup');
  await expectationInput(page, 'path').fill('');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor > [role="alert"]')).toContainText('路径');
  await expectationRow(page).getByRole('button', { name: '删除', exact: true }).click();
  const saved = await save(page, datasetId);
  expect(saved.turns[0].expectations).toEqual([sample('tool_argument').turns[0].expectations[1]]);
  await page.reload();
  await expect(page.locator('.expectation-row')).toHaveCount(0);
});

test('server replacement of the same expectation ID refreshes its raw editor value', async ({ page }) => {
  const datasetId = await open(page, 'tool_argument');
  await typeRaw(valueInput(page, 'tool_argument'), '{"local":');
  const replacement = sample('tool_argument');
  replacement.turns[0].expectations[0].condition.expected = 'server replacement';
  const updated = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/acceptance-case`, { data: replacement });
  expect(updated.ok()).toBeTruthy();
  await page.getByRole('button', { name: 'Reload server sample', exact: true }).click();
  await expect(valueInput(page, 'tool_argument')).toHaveValue(editorText('tool_argument', 'server replacement'));
  expect(await save(page, datasetId)).toEqual(replacement);
});

test('nested JSON objects remain raw while editing and persist as typed objects', async ({ page }) => {
  for (const kind of ['state', 'tool_argument'] as const) {
    const datasetId = await open(page, kind);
    const input = valueInput(page, kind);
    const expected = { items: [0, true, null, { name: 'x' }] };
    const raw = editorText(kind, expected);
    await typeRaw(input, raw);
    const saved = await save(page, datasetId);
    expect(saved.turns[0].expectations[0].condition.expected).toEqual(expected);
    await expect(input).toHaveValue(raw);
    await page.reload();
    await expect(valueInput(page, kind)).toHaveValue(raw);
    expect(await save(page, datasetId)).toEqual(saved);
  }
});

test('all three expectation kinds are created through the editor, reloaded and published with other sample fields', async ({ page }) => {
  const datasetId = await open(page);
  await page.getByLabel('样本名称', { exact: true }).fill('完整期望手动创建');
  await page.getByLabel('样本分类', { exact: true }).selectOption('boundary');
  await page.getByLabel('用户输入', { exact: true }).fill('检查完整期望');
  await page.getByLabel('期望输出', { exact: true }).fill('基本完整输出');
  await addInitializationVariable(page, 'centreId', '810000');
  await page.getByLabel('必需工具（逗号分隔）').fill('lookup');
  await page.getByLabel('禁止工具（逗号分隔）').fill('erase');
  await page.getByLabel('期望 Skill', { exact: true }).fill('review');
  await expect(page.getByText('期望结果', { exact: true })).toBeVisible();
  await expect(page.locator('.simple-expectations')).toHaveCount(0);

  const stateIndex = await addExpectation(page, 'state');
  await expectationRow(page, stateIndex).getByPlaceholder('检查名称（可选）').fill('状态确认');
  await expectationInput(page, 'path', stateIndex).fill('status');
  await expectationInput(page, 'value', stateIndex).fill('"done"');
  const toolIndex = await addExpectation(page, 'tool_argument');
  await expectationRow(page, toolIndex).getByPlaceholder('检查名称（可选）').fill('工具参数确认');
  await expectationInput(page, 'tool', toolIndex).fill('lookup');
  await expectationInput(page, 'path', toolIndex).fill('arguments.limit');
  await expectationInput(page, 'value', toolIndex).fill('10');
  const outputIndex = await addExpectation(page, 'output');
  await expectationRow(page, outputIndex).getByPlaceholder('检查名称（可选）').fill('输出字段确认');
  await expectationInput(page, 'path', outputIndex).fill('answer');
  await expectationInput(page, 'value', outputIndex).fill('"通过"');

  const saved = await save(page, datasetId, 'POST');
  expect(saved.name).toBe('完整期望手动创建');
  expect(saved.category).toBe('boundary');
  expect(saved.initial_state).toEqual({ config_variables: [{ name: 'centreId', value: '810000', type: 'str' }] });
  expect(saved.turns[0].input).toEqual({ txt: '检查完整期望' });
  expect(saved.turns[0].expectations).toEqual(expect.arrayContaining([
    expect.objectContaining({ kind: 'state', name: '状态确认', path: 'status', condition: { kind: 'equals', expected: 'done' } }),
    expect.objectContaining({ kind: 'tool_argument', name: '工具参数确认', tool: 'lookup', path: 'arguments.limit', occurrence: 'last', condition: { kind: 'equals', expected: 10 } }),
    expect.objectContaining({ kind: 'output', name: '输出字段确认', path: 'answer', condition: { kind: 'equals', expected: '通过' } }),
    expect.objectContaining({ kind: 'output', path: null, condition: { kind: 'equals', expected: '基本完整输出' } }),
    expect.objectContaining({ kind: 'skill_route', condition: { kind: 'equals', expected: 'review' } }),
    expect.objectContaining({ kind: 'tool_call', tool: 'lookup', mode: 'required' }),
    expect.objectContaining({ kind: 'tool_call', tool: 'erase', mode: 'forbidden' }),
  ]));
  expect(saved.turns[0].expectations).toHaveLength(7);
  await page.reload();
  await expect(page.locator('.expectation-row')).toHaveCount(4);
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('基本完整输出');
  await expect((await namedExpectation(page, '状态确认')).getByTestId(/^expectation-value-/)).toHaveValue('"done"');
  await expect((await namedExpectation(page, '工具参数确认')).getByTestId(/^expectation-value-/)).toHaveValue('10');
  await expect((await namedExpectation(page, '输出字段确认')).getByTestId(/^expectation-value-/)).toHaveValue('"通过"');
  expect(await save(page, datasetId)).toEqual(saved);
  await expect(page.getByTestId('add-expectation')).toHaveCSS('color', 'rgb(7, 172, 142)');
  const screenshots = await mkdtemp(join(tmpdir(), 'agentgate-expectations-review-'));
  await page.screenshot({ path: join(screenshots, 'expectations-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 720, height: 1000 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: join(screenshots, 'expectations-narrow.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  console.log('Expectation review screenshots: ' + screenshots);

  const publication = page.waitForResponse(response => response.url().endsWith('/drafts/publish'));
  await page.getByRole('button', { name: 'Publish acceptance draft' }).click();
  expect((await publication).ok()).toBeTruthy();
  await expect(page).toHaveURL(/(?:\?|&)published=1(?:&|$)/);
  await page.reload();
  for (let index = 0; index < 4; index++) {
    await expect(expectationInput(page, 'path', index)).toBeDisabled();
    await expect(expectationInput(page, 'value', index)).toBeDisabled();
  }
  await expect(page.getByTestId('add-expectation')).toHaveCount(0);
  const published = await page.request.get(baseUrl + `/api/datasets/${datasetId}/versions/1`);
  expect(published.ok()).toBeTruthy();
  expect((await published.json()).data.cases[0]).toEqual(saved);
});

test('manual creation wizard saves the same full expectation controls', async ({ page }) => {
  const datasetId = await open(page);
  await page.getByRole('button', { name: 'Open manual sample wizard', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: '下一步', exact: true }).click();
  await dialog.getByLabel('样本名称', { exact: true }).fill('向导完整期望');
  await dialog.getByLabel('用户输入', { exact: true }).fill('通过向导创建');
  await dialog.getByLabel('期望输出', { exact: true }).fill('向导基本输出');
  await expect(dialog.getByText('期望结果', { exact: true })).toBeVisible();
  const stateIndex = await dialog.locator('.expectation-row').count();
  await dialog.getByTestId('add-expectation').click();
  await page.getByRole('menuitem', { name: '最终状态', exact: true }).click();
  await dialog.getByTestId(`expectation-path-${stateIndex}`).fill('status');
  await dialog.getByTestId(`expectation-value-${stateIndex}`).fill('"done"');
  const outputIndex = await dialog.locator('.expectation-row').count();
  await dialog.getByTestId('add-expectation').click();
  await page.getByRole('menuitem', { name: '最终输出', exact: true }).click();
  await dialog.getByTestId(`expectation-path-${outputIndex}`).fill('answer');
  await dialog.getByTestId(`expectation-value-${outputIndex}`).fill('"附加答案"');
  const response = page.waitForResponse(response => response.request().method() === 'POST' &&
    response.url().includes(`/api/datasets/${datasetId}/drafts/cases`));
  await dialog.getByRole('button', { name: '保存样本', exact: true }).click();
  expect((await response).ok()).toBeTruthy();
  await expect(dialog.getByText('创建完成', { exact: false })).toBeVisible();
  const draft = await page.request.get(baseUrl + `/api/datasets/${datasetId}/drafts/current`);
  const saved = (await draft.json()).data.cases;
  expect(saved).toHaveLength(1);
  expect(saved[0].name).toBe('向导完整期望');
  expect(saved[0].turns[0].expectations).toHaveLength(3);
  expect(saved[0].turns[0].expectations).toEqual(expect.arrayContaining([
    expect.objectContaining({ kind: 'state', path: 'status', condition: { kind: 'equals', expected: 'done' } }),
    expect.objectContaining({ kind: 'output', path: 'answer', condition: { kind: 'equals', expected: '附加答案' } }),
    expect.objectContaining({ kind: 'output', path: null, condition: { kind: 'equals', expected: '向导基本输出' } }),
  ]));
});

test('primary output and additional output edits and deletions do not overwrite each other', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const primary = { id: 'primary', name: '基本输出', kind: 'output', path: null,
    condition: { kind: 'equals', expected: 'old answer' } };
  const secondary = { id: 'secondary', name: '输出正则', kind: 'output', path: 'answer',
    condition: { kind: 'matches_pattern', pattern: '^old$' } };
  const removed = { id: 'removed', name: '删除输出', kind: 'output', path: 'old_field',
    condition: { kind: 'must_be_missing' } };
  const rules = [...original.turns[0].expectations, primary, secondary, removed];
  expect((await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, {
    data: { ...original, turns: [{ ...original.turns[0], expectations: rules }] },
  })).ok()).toBeTruthy();
  await page.reload();
  await page.getByLabel('期望输出', { exact: true }).fill('new answer');
  await (await namedExpectation(page, '输出正则')).getByPlaceholder('正则表达式').fill('^new$');
  await (await namedExpectation(page, '输出正则')).getByTestId(/^expectation-path-/).fill('response.answer');
  await (await namedExpectation(page, '删除输出')).getByRole('button', { name: '删除', exact: true }).click();
  const added = await addExpectation(page, 'output');
  await expectationInput(page, 'path', added).fill('private');
  await selectCondition(page, added, '字段不存在');
  const saved = await save(page, datasetId);
  expect(saved.turns[0].expectations.slice(0, 4)).toEqual([
    ...original.turns[0].expectations,
    { ...primary, condition: { kind: 'equals', expected: 'new answer' } },
    { ...secondary, path: 'response.answer', condition: { kind: 'matches_pattern', pattern: '^new$' } },
  ]);
  expect(saved.turns[0].expectations).toHaveLength(5);
  expect(saved.turns[0].expectations[4]).toMatchObject({ kind: 'output', path: 'private', condition: { kind: 'must_be_missing' } });
  expect(saved.turns[0].expectations.some((rule: any) => rule.id === 'removed')).toBeFalsy();
  await page.reload();
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('new answer');
  await expect((await namedExpectation(page, '输出正则')).getByPlaceholder('正则表达式')).toHaveValue('^new$');
  await page.getByLabel('期望输出', { exact: true }).fill('');
  const cleared = await save(page, datasetId);
  expect(cleared.turns[0].expectations).toEqual(saved.turns[0].expectations.filter((rule: any) => rule.id !== 'primary'));
  await page.reload();
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('');
  await expect(page.locator('.expectation-row')).toHaveCount(3);
});

test('changing a rule kind retains identity and each turn keeps its own expectations', async ({ page }) => {
  const datasetId = await open(page, 'state');
  await expectationRow(page).locator('.expectation-row-head .el-select').click();
  await page.getByRole('option', { name: '工具参数', exact: true }).click();
  await expectationInput(page, 'tool').fill('lookup');
  await expectationInput(page, 'path').fill('arguments.status');
  await expectationInput(page, 'value').fill('"changed"');
  await page.getByRole('button', { name: '添加新轮次' }).click();
  await page.getByLabel('用户输入', { exact: true }).fill('第二轮');
  const secondRule = await addExpectation(page, 'output');
  await expectationInput(page, 'path', secondRule).fill('ok');
  await expectationInput(page, 'value', secondRule).fill('true');
  await page.getByRole('button', { name: /第 1 轮/ }).click();
  await expect(expectationInput(page, 'tool')).toHaveValue('lookup');
  await expect(expectationInput(page, 'value')).toHaveValue('"changed"');
  const saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0]).toEqual({
    ...sample('state').turns[0].expectations[0], kind: 'tool_argument', tool: 'lookup',
    occurrence: 'last', path: 'arguments.status', condition: { kind: 'equals', expected: 'changed' },
  });
  expect(saved.turns[0].expectations[1]).toEqual(sample('state').turns[0].expectations[1]);
  expect(saved.turns[1].expectations).toEqual([
    expect.objectContaining({ kind: 'output', path: 'ok', condition: { kind: 'equals', expected: true } }),
  ]);
  await page.reload();
  await page.getByRole('button', { name: /第 2 轮/ }).click();
  await expect(expectationInput(page, 'value')).toHaveValue('true');
  await expectationInput(page, 'value').fill('[1,');
  await page.getByRole('button', { name: /第 1 轮/ }).click();
  await expect(page.getByLabel('用户输入', { exact: true })).toHaveValue('第二轮');
  await expect(expectationInput(page, 'value')).toHaveValue('[1,');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor > [role="alert"]')).toContainText('JSON');
  await expectationRow(page).getByRole('button', { name: '删除', exact: true }).click();
  const removed = await save(page, datasetId);
  expect(removed.turns[0]).toEqual(saved.turns[0]);
  expect(removed.turns[1].expectations).toEqual([]);
});

test('non-equality output rules stay visible and leave the optional basic output empty', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const original = sample('state');
  const secondary = [
    { id: 'regex-full-output', name: '完整输出模式', kind: 'output', path: null,
      condition: { kind: 'matches_pattern', pattern: '(?P<answer>yes|no)' } },
    { id: 'range-output', name: '评分范围', kind: 'output', path: 'score',
      condition: { kind: 'within_range', minimum: 0, maximum: 1 } },
    { id: 'set-output', name: '集合检查', kind: 'output', path: 'choice',
      condition: { kind: 'one_of', allowed: ['a,b', [0, false, null], { nested: { enabled: true } }] } },
    { id: 'schema-output', name: '输出结构', kind: 'output', path: 'answer',
      condition: { kind: 'matches_json_schema', json_schema: { type: 'object', required: ['text'] } } },
  ];
  const seeded = { ...original, turns: [{ ...original.turns[0], expectations: [...original.turns[0].expectations, ...secondary] }] };
  const seededResponse = await page.request.put(baseUrl + `/api/datasets/${datasetId}/drafts/cases/${original.id}`, { data: seeded });
  expect(seededResponse.ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('');
  await expect(page.locator('.expectation-row')).toHaveCount(5);
  await expect(expectationRow(page, 1).getByPlaceholder('正则表达式')).toHaveValue('(?P<answer>yes|no)');
  expect(JSON.parse(await expectationInput(page, 'allowed', 3).inputValue())).toEqual(secondary[2].condition.allowed);
  expect(JSON.parse(await expectationInput(page, 'schema', 4).inputValue())).toEqual(secondary[3].condition.json_schema);
  expect(await save(page, datasetId)).toEqual(seeded);
  await expectationInput(page, 'path', 1).fill(' ');
  await expectationInput(page, 'allowed', 3).fill('[0,false,"text,with,commas",[1,2],{"limit":2}]');
  await expectationInput(page, 'schema', 4).fill('{"type":"object","required":["answer"]}');
  const edited = await save(page, datasetId);
  expect(edited.turns[0].expectations.find((rule: any) => rule.id === 'regex-full-output')).toEqual(secondary[0]);
  expect(edited.turns[0].expectations.find((rule: any) => rule.id === 'set-output')?.condition)
    .toEqual({ kind: 'one_of', allowed: [0, false, 'text,with,commas', [1, 2], { limit: 2 }] });
  expect(edited.turns[0].expectations.find((rule: any) => rule.id === 'schema-output')?.condition)
    .toEqual({ kind: 'matches_json_schema', json_schema: { type: 'object', required: ['answer'] } });
  await page.reload();
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('');
  expect(await save(page, datasetId)).toEqual(edited);
});

test('condition controls validate raw collections, numeric bounds and schema without losing rule identity', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const row = expectationRow(page);
  const error = page.locator('.sample-editor > [role="alert"]');
  const clickSave = () => page.getByRole('button', { name: '保存样本', exact: true }).click();
  const fillNumber = async (placeholder: string, value: string) => {
    const input = row.getByPlaceholder(placeholder, { exact: true });
    await input.fill(value);
    await input.press('Tab');
  };
  await selectCondition(page, 0, '属于集合');
  await expectationInput(page, 'allowed').fill('[1,');
  await clickSave();
  await expect(error).toContainText('JSON');
  await expect(expectationInput(page, 'allowed')).toHaveValue('[1,');
  await expect(page.getByTestId('dirty')).toHaveText('true');
  await expectationInput(page, 'allowed').fill('[]');
  await clickSave();
  await expect(error).not.toBeEmpty();
  await expectationInput(page, 'allowed').fill('[{"a":1,"b":2},{"b":2,"a":1}]');
  await clickSave();
  await expect(error).toContainText('不能重复');
  await expectationInput(page, 'allowed').fill('[0,false,"a,b",[1,2]]');
  let saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0]).toEqual({
    ...sample('state').turns[0].expectations[0],
    condition: { kind: 'one_of', allowed: [0, false, 'a,b', [1, 2]] },
  });
  await selectCondition(page, 0, '数值范围');
  await clickSave();
  await expect(error).not.toBeEmpty();
  await fillNumber('最小值', '10');
  await fillNumber('最大值', '5');
  await clickSave();
  await expect(error).not.toBeEmpty();
  await fillNumber('最小值', '1');
  saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0].condition).toEqual({ kind: 'within_range', minimum: 1, maximum: 5 });
  await selectCondition(page, 0, '数值容差');
  await fillNumber('期望值', '4.5');
  await fillNumber('容差', '0.2');
  saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0].condition).toEqual({ kind: 'within_tolerance', expected: 4.5, epsilon: 0.2 });
  await selectCondition(page, 0, '正则匹配');
  await row.getByPlaceholder('正则表达式').fill('^done$');
  saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0].condition).toEqual({ kind: 'matches_pattern', pattern: '^done$' });
  await selectCondition(page, 0, '字段不存在');
  saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0].condition).toEqual({ kind: 'must_be_missing' });
  await selectCondition(page, 0, 'JSON Schema');
  await expectationInput(page, 'schema').fill('{"type":');
  await clickSave();
  await expect(error).toContainText('JSON');
  await expectationInput(page, 'schema').fill('[]');
  await clickSave();
  await expect(error).not.toBeEmpty();
  await expectationInput(page, 'schema').fill('{"type":"string"}');
  saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0]).toEqual({
    ...sample('state').turns[0].expectations[0], condition: { kind: 'matches_json_schema', json_schema: { type: 'string' } },
  });
  expect(saved.turns[0].expectations[1]).toEqual(sample('state').turns[0].expectations[1]);
});

test('state paths are required while plain text equality and an omitted check name remain supported', async ({ page }) => {
  const datasetId = await open(page, 'state');
  await expectationRow(page).getByPlaceholder('检查名称（可选）').fill('');
  await expectationInput(page, 'path').fill(' ');
  await expectationInput(page, 'value').fill('普通文本，无需 JSON 引号');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor > [role="alert"]')).toContainText('路径');
  await expectationInput(page, 'path').fill('status');
  const saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0]).toEqual({
    ...sample('state').turns[0].expectations[0], name: null,
    condition: { kind: 'equals', expected: '普通文本，无需 JSON 引号' },
  });
  await page.reload();
  await expect(expectationInput(page, 'value')).toHaveValue('"普通文本，无需 JSON 引号"');
});

test('full-output rules keep their editing controls and synchronize with the basic output without duplication', async ({ page }) => {
  const datasetId = await open(page);
  await page.getByLabel('用户输入', { exact: true }).fill('完整输出生命周期');
  const index = await addExpectation(page, 'output');
  await expectationRow(page, index).getByPlaceholder('检查名称（可选）').fill('完整输出规则');
  await expectationInput(page, 'value', index).fill('"first answer"');
  const created = await save(page, datasetId, 'POST');
  expect(created.turns[0].expectations).toHaveLength(1);
  const originalRule = created.turns[0].expectations[0];
  expect(originalRule).toMatchObject({ kind: 'output', name: '完整输出规则', path: null,
    condition: { kind: 'equals', expected: 'first answer' } });
  await page.reload();
  await expect(page.locator('.expectation-row')).toHaveCount(1);
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('first answer');
  await expect(expectationRow(page).getByRole('button', { name: '删除', exact: true })).toBeVisible();
  await expect(expectationRow(page).getByPlaceholder('检查名称（可选）')).toHaveValue('完整输出规则');
  await page.getByLabel('期望输出', { exact: true }).fill('second answer');
  let saved = await save(page, datasetId);
  expect(saved.turns[0].expectations).toEqual([
    { ...originalRule, condition: { kind: 'equals', expected: 'second answer' } },
  ]);
  await expect(expectationInput(page, 'value')).toHaveValue('"second answer"');
  await expectationInput(page, 'value').fill('"third answer"');
  saved = await save(page, datasetId);
  expect(saved.turns[0].expectations).toEqual([
    { ...originalRule, condition: { kind: 'equals', expected: 'third answer' } },
  ]);
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('third answer');
  await expectationRow(page).getByPlaceholder('检查名称（可选）').fill('完整输出模式');
  await selectCondition(page, 0, '正则匹配');
  await expectationRow(page).getByPlaceholder('正则表达式').fill('^third');
  saved = await save(page, datasetId);
  expect(saved.turns[0].expectations).toEqual([
    { ...originalRule, name: '完整输出模式', condition: { kind: 'matches_pattern', pattern: '^third' } },
  ]);
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('');
  await expect(expectationRow(page).getByPlaceholder('正则表达式')).toHaveValue('^third');
  await expectationInput(page, 'path').fill('answer');
  saved = await save(page, datasetId);
  expect(saved.turns[0].expectations[0].path).toBe('answer');
  expect(saved.turns[0].expectations[0].id).toBe(originalRule.id);
  await expectationRow(page).getByRole('button', { name: '删除', exact: true }).click();
  expect((await save(page, datasetId)).turns[0].expectations).toEqual([]);
  await page.reload();
  await expect(page.locator('.expectation-row')).toHaveCount(0);
  await expect(page.getByLabel('期望输出', { exact: true })).toHaveValue('');
  await addExpectation(page, 'output');
  const emptyOutput = await save(page, datasetId);
  expect(emptyOutput.turns[0].expectations).toEqual([
    expect.objectContaining({ kind: 'output', path: null, condition: { kind: 'equals', expected: '' } }),
  ]);
  await page.reload();
  await expect(page.locator('.expectation-row')).toHaveCount(1);
  await expect(expectationInput(page, 'value')).toHaveValue('""');
});

test('non-finite JSON numbers cannot silently become null in saved expectation values', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const submitted: string[] = [];
  page.on('request', request => {
    if (request.method() === 'PUT' && request.url().includes(`/api/datasets/${datasetId}/drafts/cases`))
      submitted.push(request.url());
  });
  for (const raw of ['1e400', '-1e400', '[1e400]', '{"value":1e400}', '{"nested":[1e400]}']) {
    await expectationInput(page, 'value').fill(raw);
    await page.getByRole('button', { name: '保存样本', exact: true }).click();
    await expect(page.locator('.sample-editor > [role="alert"]')).not.toBeEmpty();
    await expect(expectationInput(page, 'value')).toHaveValue(raw);
    await expect(page.getByTestId('dirty')).toHaveText('true');
    await page.getByRole('button', { name: '添加新轮次' }).click();
    await expect(page.locator('.round')).toHaveCount(1);
  }
  expect(submitted).toEqual([]);
  const persisted = await page.request.get(baseUrl + `/api/datasets/${datasetId}/drafts/current`);
  expect((await persisted.json()).data.cases[0]).toEqual(sample('state'));
  await expectationInput(page, 'value').fill('7');
  expect((await save(page, datasetId)).turns[0].expectations[0].condition)
    .toEqual({ kind: 'equals', expected: 7 });
});

test('editing the basic output preserves an invalid JSON draft in another rule and keeps saving blocked', async ({ page }) => {
  const datasetId = await open(page, 'state');
  const submitted: string[] = [];
  page.on('request', request => {
    if (request.method() === 'PUT' && request.url().includes(`/api/datasets/${datasetId}/drafts/cases`))
      submitted.push(request.url());
  });
  const unfinished = '{"unfinished":';
  await expectationInput(page, 'value').fill(unfinished);
  await page.getByLabel('期望输出', { exact: true }).fill('new basic answer');
  await expect(expectationInput(page, 'value')).toHaveValue(unfinished);
  await expect(page.getByTestId('dirty')).toHaveText('true');
  await page.getByRole('button', { name: '保存样本', exact: true }).click();
  await expect(page.locator('.sample-editor > [role="alert"]')).toContainText('JSON');
  await page.getByRole('button', { name: '添加新轮次' }).click();
  await expect(page.locator('.round')).toHaveCount(1);
  await expect(expectationInput(page, 'value')).toHaveValue(unfinished);
  expect(submitted).toEqual([]);
  await expectationInput(page, 'value').fill('"fixed state"');
  const saved = await save(page, datasetId);
  expect(saved.turns[0].expectations).toEqual(expect.arrayContaining([
    { ...sample('state').turns[0].expectations[0], condition: { kind: 'equals', expected: 'fixed state' } },
    expect.objectContaining({ kind: 'output', path: null, condition: { kind: 'equals', expected: 'new basic answer' } }),
  ]));
  expect(saved.turns[0].expectations).toHaveLength(3);
});
