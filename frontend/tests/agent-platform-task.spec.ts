import { test, expect, type Page, type Route } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath } from 'node:url';

let server: ViteDevServer;
let url: string;
const fixture = `<!doctype html><html><body><div id="app"></div><script type="module">
import {createApp,h,ref} from 'vue';
import {agentDirectory} from '/src/api/agent-platform.ts';
import Picker from '/src/views/evaluation/components/AgentTargetPicker.vue';
import '/node_modules/element-plus/dist/index.css';
window.directory=agentDirectory;
window.callDirectory=async(name,input)=>{try{return {ok:true,value:await agentDirectory[name](input)}}catch(error){return {ok:false,error:{status:error.status,kind:error.kind,message:error.message,properties:Object.keys(error),cause:error.cause}}}};
createApp({setup(){const picker=ref(null),token=ref('test-token'),teamId=ref('a');window.readTarget=()=>picker.value?.readSubmissionSelection();window.setAuth=auth=>{token.value=auth.token;teamId.value=auth.teamId;};return()=>h(Picker,{ref:picker,directory:agentDirectory,token:token.value,teamId:teamId.value});}}).mount('#app');
</script></body></html>`;

const formFixture = `<!doctype html><html><body><div id="app"></div><script type="module">
import {createApp,h} from 'vue';
import ElementPlus from 'element-plus';
import {pinia} from '/src/stores/index.ts';
import {useAuthStore} from '/src/stores/modules/auth';
import {useTaskLinksStore} from '/src/stores/modules/task-links';
import Form from '/src/views/evaluation/components/EvaluationTaskForm.vue';
import '/node_modules/element-plus/dist/index.css';
import '/src/styles/main.scss';
import '/src/views/evaluation/index.vue?vue&type=style&index=0&lang.css';
window.created=[];window.updates=0;
window.readTaskLinks=()=>useTaskLinksStore().readTaskLinks();
window.addEventListener('task-links-updated',()=>window.updates++);
window.setAuth=state=>{const auth=useAuthStore();if(state==='logout')auth.$reset();else auth.$patch(state);};
window.initializePlatform=()=>useAuthStore().initializePlatform({token:'integrated-secret',teamId:'team',teamName:'集成测试团队',userId:'integrated-user',userName:'集成测试用户'});
window.readAuth=()=>{const auth=useAuthStore();return {loginMode:auth.loginMode,platformReady:auth.platformReady,usesPlatformDirectory:auth.usesPlatformDirectory,userId:auth.userId,teamName:auth.teamName};};
const source={id:'dataset',version:2,evaluatorId:'judge',targetVersion:'old-1',...(location.search.includes('selected')?{caseIds:['case-1']}: {})};
const app=createApp({render:()=>h(Form,{source,onCreated:link=>window.created.push(link)})}).use(pinia).use(ElementPlus);
if(location.search.includes('integrated'))window.initializePlatform();
else useAuthStore().$patch(location.search.includes('external')
  ? {loginMode:'external',token:'',teamId:'',teamName:''}
  : {loginMode:'bank',token:'form-secret',teamId:'team',teamName:'测试团队'});
app.mount('#app');
</script></body></html>`;

async function startServer(origins?: { platform: string; claw: string }) {
  const vite = await createServer({
    configFile: false,
    root: fileURLToPath(new URL('..', import.meta.url)),
    // Keep test builds independent from a developer's environment configuration.
    define: {
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify('/api'),
      'import.meta.env.VITE_AGENT_PLATFORM_ORIGIN': JSON.stringify(origins?.platform ?? ''),
      'import.meta.env.VITE_ABCCLAW_PLATFORM_ORIGIN': JSON.stringify(origins?.claw ?? ''),
    },
    plugins: [
      vue(),
      {
        name: 'platform-directory-test-page',
        configureServer(instance) {
          instance.middlewares.use('/__form', async (_request, response, next) => {
            try {
              response.setHeader('Content-Type', 'text/html');
              response.end(await instance.transformIndexHtml('/__form', formFixture));
            } catch (error) {
              next(error);
            }
          });
          instance.middlewares.use('/__directory', async (_request, response, next) => {
            try {
              response.setHeader('Content-Type', 'text/html');
              response.end(await instance.transformIndexHtml('/__directory', fixture));
            } catch (error) {
              next(error);
            }
          });
        },
      },
    ],
    server: { host: '127.0.0.1', port: 0 },
  });
  await vite.listen();
  const address = vite.httpServer!.address();
  if (!address || typeof address === 'string') throw Error('No test server address');
  return { server: vite, url: `http://127.0.0.1:${address.port}/__directory` };
}
test.beforeAll(async () => {
  ({ server, url } = await startServer());
});
test.afterAll(async () => {
  await server?.close();
});
test.beforeEach(async ({ page }) => {
  await page.goto(url);
  await page.waitForFunction(() => !!(window as any).directory);
});

const wrap = (data: unknown) => ({ code: '0', message: '查询成功', data });
const bankVersionOne = '2026-05-06 17:21:16';
const bankVersionTwo = '2026-05-06 17:20:34';
const batch = (records: unknown[], total = records.length, current = 1, size = 300) => ({
  records,
  total,
  current,
  size,
  pages: Math.ceil(total / size),
});
const reply = (route: Route, value: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
const call = (page: Page, name: string, input: Record<string, unknown> = { token: 'test-token' }) =>
  page.evaluate(({ name, input }) => (window as any).callDirectory(name, input), { name, input });
async function choose(page: Page, label: string, option: string) {
  await page.getByRole('combobox', { name: label, exact: true }).press('Enter');
  await page
    .getByRole('option', {
      name: label === '选择智能体' ? option.split(' · ')[0] : option,
      exact: true,
    })
    .click();
}

test('complete paginated team membership uses teamId, deduplicates identical rows and omits browser cookies', async ({
  page,
  context,
}) => {
  await context.addCookies([{ name: 'platform-session', value: 'unrelated-session', url }]);
  const requests: {
    path: string;
    query: Record<string, string>;
    authorization?: string;
    cookie?: string;
  }[] = [];
  await page.route('**/web/**', async (route) => {
    const parsed = new URL(route.request().url());
    const headers = await route.request().allHeaders();
    requests.push({
      path: parsed.pathname,
      query: Object.fromEntries(parsed.searchParams),
      authorization: headers.authorization,
      cookie: headers.cookie,
    });
    const current = Number(parsed.searchParams.get('page'));
    const records =
      current === 1
        ? [
            { id: 'membership-1', teamId: 'a', teamName: '团队甲' },
            { id: 'membership-2', teamId: 'a', teamName: '团队甲' },
          ]
        : [{ id: 'membership-3', teamId: 'b', teamName: '团队乙' }];
    await reply(route, wrap(batch(records, 3, current, 2)));
  });
  expect(await call(page, 'getTeams')).toEqual({
    ok: true,
    value: [
      { teamId: 'a', teamName: '团队甲' },
      { teamId: 'b', teamName: '团队乙' },
    ],
  });
  expect(requests).toEqual(
    [1, 2].map((page) => ({
      path: '/web/ops/team/getTeamRole',
      query: { limit: '300', page: String(page) },
      authorization: 'Bearer test-token',
      cookie: undefined,
    })),
  );
});

test('direct agent pages encode IDs and prefer arrangeType when normalizing type fields', async ({
  page,
}) => {
  const types = [
    ['base', 'base'],
    ['base', 'workflow'],
    ['abcclaw', 'abcclaw2'],
    [null, 'workflow'],
    ['base', 'abcclaw'],
    ['light', 'base'],
    [null, null],
  ];
  const received: URL[] = [];
  await page.route('**/web/**', async (route) => {
    const parsed = new URL(route.request().url());
    received.push(parsed);
    const current = Number(parsed.searchParams.get('page'));
    await reply(
      route,
      batch(
        types.slice((current - 1) * 3, current * 3).map(([agentType, arrangeType], index) => ({
          id: String((current - 1) * 3 + index),
          name: '智能体',
          agentType,
          arrangeType,
        })),
        types.length,
        current,
        3,
      ),
    );
  });
  const result = await call(page, 'getAgents', { token: 'test-token', teamId: '团队 &/+#?' });
  expect(result.ok).toBe(true);
  expect(result.value.map((agent: any) => agent.typeGroup)).toEqual([
    'base/workflow',
    'base/workflow',
    'abcclaw',
    'base/workflow',
    'abcclaw',
    'base/workflow',
    null,
  ]);
  expect(result.value[2]).toMatchObject({
    platformAgentType: 'abcclaw',
    platformArrangeType: 'abcclaw2',
  });
  expect(received).toHaveLength(3);
  for (const parsed of received) {
    expect(parsed.pathname).toBe('/web/agent/agents');
    expect(parsed.searchParams.get('teamId')).toBe('团队 &/+#?');
    expect(parsed.searchParams.get('limit')).toBe('1000');
    expect(parsed.searchParams.get('name')).toBe('');
  }
});

test('both version endpoints retain statuses; branch tree preserves nested identity and leaf defaults', async ({
  page,
}) => {
  const requests: URL[] = [];
  await page.route('**/web/**', async (route) => {
    const parsed = new URL(route.request().url());
    requests.push(parsed);
    const data = parsed.pathname.endsWith('branchTree')
      ? [{ branchId: 'root', branchName: '主分支', children: [{ branchId: 'branch &/+' }] }]
      : parsed.pathname.endsWith('listVersions')
        ? [{ agentVersion: '1.0', status: 'test', branchId: 'branch &/+' }]
        : [
            { agentVersion: null, updatedAt: '2026-05-06 17:21:16', status: 'published' },
            { agentVersion: null, updatedAt: '2026-05-06 17:20:34', status: 'draft' },
          ];
    await reply(route, wrap(data));
  });
  expect(await call(page, 'getBranches', { token: 't', agentId: 'claw/1' })).toEqual({
    ok: true,
    value: [
      {
        branchId: 'root',
        branchName: '主分支',
        children: [{ branchId: 'branch &/+', branchName: null, children: [] }],
      },
    ],
  });
  expect(await call(page, 'getAgentVersions', { token: 't', agentId: 'base/1' })).toEqual({
    ok: true,
    value: [
      { agentVersion: '2026-05-06 17:21:16', status: 'published' },
      { agentVersion: '2026-05-06 17:20:34', status: 'draft' },
    ],
  });
  expect(
    await call(page, 'getBranchVersions', {
      token: 't',
      agentId: 'claw/1',
      branchId: 'branch &/+',
    }),
  ).toEqual({ ok: true, value: [{ agentVersion: '1.0', status: 'test' }] });
  expect(requests.map((item) => item.pathname)).toEqual([
    '/web/abcclaw/v2/branchTree',
    '/web/agent/getAgentVersionList',
    '/web/abcclaw/v2/listVersions',
  ]);
  expect(Object.fromEntries(requests[1].searchParams)).toEqual({ agentId: 'base/1' });
  expect(Object.fromEntries(requests[2].searchParams)).toEqual({
    agentId: 'claw/1',
    branchId: 'branch &/+',
  });
});

test('in-bank base/workflow versions use updatedAt while branch versions keep agentVersion', async ({ page }) => {
  await page.route('**/web/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    return reply(route, wrap(path.endsWith('getAgentVersionList')
      ? [
          { agentVersion: null, updatedAt: '2026-05-06 17:21:16', status: 'published' },
          { agentVersion: 'ignored', updatedAt: '2026-05-06 17:20:34', status: 'draft' },
        ]
      : [{ agentVersion: 'claw-v1', updatedAt: '2026-05-06 17:21:16',
          status: 'test', branchId: 'branch-1' }]));
  });

  expect(await call(page, 'getAgentVersions', { token: 't', agentId: 'base-1' })).toEqual({
    ok: true,
    value: [
      { agentVersion: '2026-05-06 17:21:16', status: 'published' },
      { agentVersion: '2026-05-06 17:20:34', status: 'draft' },
    ],
  });
  expect(await call(page, 'getBranchVersions', {
    token: 't', agentId: 'claw-1', branchId: 'branch-1',
  })).toEqual({ ok: true, value: [{ agentVersion: 'claw-v1', status: 'test' }] });
});

for (const arrangeType of ['base', 'workflow']) {
  test(`in-bank ${arrangeType} picker accepts null status and selects updatedAt`, async ({ page }) => {
    await page.route('**/web/**', (route) => {
      const path = new URL(route.request().url()).pathname;
      return reply(
        route,
        path.endsWith('/agents')
          ? batch([{ id: 'bank-agent', name: '行内智能体', agentType: arrangeType, arrangeType }])
          : wrap([{ agentVersion: null, updatedAt: '2026-05-06 17:21:16', status: null }]),
      );
    });
    expect(await call(page, 'getAgentVersions', { token: 't', agentId: 'bank-agent' })).toEqual({
      ok: true,
      value: [{ agentVersion: '2026-05-06 17:21:16' }],
    });
    await choose(page, '选择智能体', '行内智能体');
    await choose(page, '智能体版本', '2026-05-06 17:21:16');
    expect(await page.evaluate(() => (window as any).readTarget())).toMatchObject({
      target: {
        agentId: 'bank-agent',
        typeGroup: 'base/workflow',
        platformArrangeType: arrangeType,
        agentVersion: '2026-05-06 17:21:16',
      },
    });
    await expect(page.getByRole('alert')).toHaveCount(0);
  });
}

test('in-bank base/workflow versions reject blank updatedAt even if agentVersion is present', async ({ page }) => {
  await page.route('**/web/**', (route) => reply(route, wrap([
    { agentVersion: 'legacy', updatedAt: ' ', status: 'published' },
  ])));
  expect(await call(page, 'getAgentVersions', { token: 't', agentId: 'base-1' }))
    .toMatchObject({ ok: false, error: { kind: 'protocol', status: 200 } });
});

test('a failed or inconsistent subsequent page never returns partial success', async ({ page }) => {
  for (const mode of ['http', 'repeated-page', 'changed-total', 'early-empty']) {
    await page.route('**/web/**', async (route) => {
      const current = Number(new URL(route.request().url()).searchParams.get('page'));
      if (current === 1)
        return reply(route, wrap(batch([{ teamId: 'a', teamName: '甲' }], 2, 1, 1)));
      if (mode === 'http') return reply(route, { message: 'test-token' }, 503);
      const data = batch(
        mode === 'early-empty' ? [] : [{ teamId: 'b', teamName: '乙' }],
        mode === 'changed-total' ? 3 : 2,
        mode === 'repeated-page' ? 1 : 2,
        1,
      );
      await reply(route, wrap(data));
    });
    const result = await call(page, 'getTeams');
    expect(result).toMatchObject({
      ok: false,
      error: { kind: mode === 'http' ? 'http' : 'protocol' },
    });
    expect(result).not.toHaveProperty('value');
    expect(JSON.stringify(result)).not.toContain('test-token');
    await page.unroute('**/web/**');
  }
});

test('accepts documented empty pages but rejects malformed envelopes and ambiguous data', async ({
  page,
}) => {
  for (const pages of [0, 1]) {
    await page.route('**/web/**', (route) => reply(route, wrap({ ...batch([]), pages })));
    expect(await call(page, 'getTeams')).toEqual({ ok: true, value: [] });
    await page.unroute('**/web/**');
  }
  const cases = [
    {
      name: 'getTeams',
      data: wrap(
        batch([
          { teamId: 'a', teamName: '甲' },
          { teamId: 'a', teamName: '乙' },
        ]),
      ),
    },
    { name: 'getTeams', data: wrap({ ...batch([]), current: '1' }) },
    { name: 'getTeams', data: wrap({ ...batch([]), size: 0 }) },
    { name: 'getTeams', data: { code: 0, message: 'ok', data: batch([]) } },
    { name: 'getTeams', data: batch([]) },
    { name: 'getTeams', data: wrap(batch([{ id: 'membership-id', teamName: '甲' }])) },
    { name: 'getAgents', data: wrap(batch([])) },
    { name: 'getBranches', data: wrap([{ branchId: 'same', children: [{ branchId: 'same' }] }]) },
    { name: 'getBranches', data: wrap([{ branchId: 'a', children: null }]) },
    { name: 'getBranchVersions', data: wrap([{ agentVersion: '1', branchId: 'wrong' }]) },
    { name: 'getBranchVersions', data: wrap([{ agentVersion: '1' }]) },
    {
      name: 'getAgentVersions',
      data: wrap([
        { agentVersion: null, updatedAt: '2026-05-06 17:21:16', status: 'draft' },
        { agentVersion: null, updatedAt: '2026-05-06 17:21:16', status: 'published' },
      ]),
    },
    { name: 'getAgentVersions', data: wrap([{ agentVersion: null, updatedAt: '' }]) },
    { name: 'getAgentVersions', data: wrap([{ agentVersion: null, updatedAt: '2026-05-06 17:21:16', status: 1 }]) },
    { name: 'getBranchVersions', data: wrap([{ agentVersion: '1', branchId: 'a', status: null }]) },
  ];
  for (const item of cases) {
    await page.route('**/web/**', (route) => reply(route, item.data));
    expect(
      await call(page, item.name, { token: 't', teamId: 'a', agentId: 'a', branchId: 'a' }),
    ).toMatchObject({ ok: false, error: { kind: 'protocol', status: 200 } });
    await page.unroute('**/web/**');
  }
});

test('all error channels are sanitized and HTTP authentication status is retained', async ({
  page,
}) => {
  for (const status of [401, 403, 500]) {
    await page.route('**/web/**', (route) =>
      reply(route, { message: 'Authorization: Bearer sensitive-token' }, status),
    );
    const result = await call(page, 'getTeams', { token: 'sensitive-token' });
    expect(result).toMatchObject({
      ok: false,
      error: { kind: 'http', status, properties: ['status', 'kind'] },
    });
    expect(JSON.stringify(result)).not.toContain('sensitive-token');
    await page.unroute('**/web/**');
  }
  await page.route('**/web/**', (route) =>
    reply(route, { code: '1', message: 'sensitive-token', data: null }),
  );
  expect(await call(page, 'getTeams')).toMatchObject({
    ok: false,
    error: { kind: 'business', status: 200 },
  });
  await page.unroute('**/web/**');
  await page.route('**/web/**', (route) => route.fulfill({ body: 'invalid JSON sensitive-token' }));
  expect(await call(page, 'getTeams')).toMatchObject({
    ok: false,
    error: { kind: 'protocol', status: 200 },
  });
  await page.unroute('**/web/**');
  await page.route('**/web/**', (route) => route.abort('failed'));
  expect(await call(page, 'getTeams')).toMatchObject({
    ok: false,
    error: { kind: 'network', status: 0 },
  });
});

test('invalid inputs cause no request and concurrent calls cannot exchange tokens', async ({
  page,
}) => {
  const requests: { token?: string; agentId: string | null }[] = [];
  await page.route('**/web/**', async (route) => {
    requests.push({
      token: (await route.request().allHeaders()).authorization,
      agentId: new URL(route.request().url()).searchParams.get('agentId'),
    });
    await reply(route, wrap([{ agentVersion: null, updatedAt: '2026-05-06 17:21:16' }]));
  });
  for (const token of ['', ' ', ' leading', 'line\nbreak', 'Bearer abc', 'x'.repeat(513)]) {
    expect(await call(page, 'getTeams', { token })).toMatchObject({
      ok: false,
      error: { kind: 'invalid_input', status: 0 },
    });
  }
  expect(await call(page, 'getAgents', { token: 't', teamId: ' ' })).toMatchObject({
    ok: false,
    error: { kind: 'invalid_input' },
  });
  expect(requests).toEqual([]);
  const results = await Promise.all([
    call(page, 'getAgentVersions', { token: 'first-token', agentId: 'one' }),
    call(page, 'getAgentVersions', { token: 'second-token', agentId: 'two' }),
  ]);
  expect(results.every((result) => result.ok)).toBe(true);
  expect(requests).toEqual(
    expect.arrayContaining([
      { token: 'Bearer first-token', agentId: 'one' },
      { token: 'Bearer second-token', agentId: 'two' },
    ]),
  );
});

test('one 30-second deadline spans all pagination requests and aborts the outstanding fetch', async ({
  page,
}) => {
  await page.clock.install();
  const requests: Route[] = [];
  await page.route('**/web/**', (route) => {
    requests.push(route);
  });
  await page.evaluate(() => {
    (window as any).pendingDirectory = (window as any).callDirectory('getTeams', { token: 't' });
  });
  await expect.poll(() => requests.length).toBe(1);
  await page.clock.runFor(20000);
  await reply(requests[0], wrap(batch([{ teamId: 'a', teamName: '甲' }], 2, 1, 1)));
  await expect.poll(() => requests.length).toBe(2);
  const aborted = page.waitForEvent('requestfailed', (request) => request.url().includes('page=2'));
  await page.clock.runFor(10001);
  expect(await page.evaluate(() => (window as any).pendingDirectory)).toMatchObject({
    ok: false,
    error: { kind: 'timeout', status: 0 },
  });
  await aborted;
});

test('separate configured origins route the correct endpoint families', async ({ page }) => {
  const configured = await startServer({
    platform: 'https://platform.example',
    claw: 'https://claw.example',
  });
  try {
    await page.goto(configured.url);
    await page.waitForFunction(() => !!(window as any).directory);
    const requests: string[] = [];
    await page.route('https://*.example/web/**', async (route) => {
      requests.push(route.request().url());
      await reply(route, wrap([]));
    });
    expect((await call(page, 'getAgentVersions', { token: 't', agentId: 'a' })).ok).toBe(true);
    expect((await call(page, 'getBranches', { token: 't', agentId: 'a' })).ok).toBe(true);
    expect(requests).toEqual([
      'https://platform.example/web/agent/getAgentVersionList?agentId=a',
      'https://claw.example/web/abcclaw/v2/branchTree?agentId=a',
    ]);
  } finally {
    await configured.server.close();
  }
});

test('invalid origin configuration and HTTP redirects cannot redirect credentials', async ({
  page,
}) => {
  for (const platform of [
    'https://name:secret@example.com',
    'https://example.com/web',
    'https://example.com/path/..',
    'https://example.com?x=1',
    'ftp://example.com',
  ]) {
    const configured = await startServer({ platform, claw: '' });
    try {
      await page.goto(configured.url);
      await page.waitForFunction(() => !!(window as any).directory);
      expect(await call(page, 'getTeams')).toMatchObject({
        ok: false,
        error: { kind: 'configuration', status: 0 },
      });
    } finally {
      await configured.server.close();
    }
  }
  await page.goto(url);
  await page.waitForFunction(() => !!(window as any).directory);
  let redirected = false;
  await page.route('**/redirect-destination', async (route) => {
    redirected = true;
    await reply(route, wrap(batch([])));
  });
  await page.route('**/web/**', (route) =>
    route.fulfill({ status: 302, headers: { Location: '/redirect-destination' } }),
  );
  expect(await call(page, 'getTeams')).toMatchObject({ ok: false, error: { kind: 'network' } });
  expect(redirected).toBe(false);
});

test('real picker consumes the HTTP directory through an exact abcclaw version', async ({
  page,
}) => {
  await page.route('**/web/**', async (route) => {
    const parsed = new URL(route.request().url());
    const data = parsed.pathname.endsWith('getTeamRole')
      ? wrap(batch([{ teamId: 'a', teamName: '测试团队' }]))
      : parsed.pathname.endsWith('/agents')
        ? batch([{ id: 'claw', name: '测试云虾', agentType: 'abcclaw', arrangeType: 'abcclaw2' }])
        : parsed.pathname.endsWith('branchTree')
          ? wrap([
              {
                branchId: 'root',
                branchName: '主分支',
                children: [{ branchId: 'leaf', branchName: '测试分支' }],
              },
            ])
          : wrap([{ agentVersion: '1.2', status: 'test', branchId: 'leaf' }]);
    await reply(route, data);
  });
  await choose(page, '选择智能体', '测试云虾 · claw');
  await choose(page, '分支地址', '↳ 测试分支 · leaf');
  await choose(page, '智能体版本', '1.2 · test');
  expect(await page.evaluate(() => (window as any).readTarget())).toMatchObject({
    token: 'test-token',
    target: {
      agentId: 'claw',
      branchId: 'leaf',
      agentVersion: '1.2',
      platformArrangeType: 'abcclaw2',
    },
  });
});

const sampleCases = [
  {
    id: 'case-1',
    name: '样本一',
    turns: [{ input: { txt: '问题一' }, expectations: [{ kind: 'output' }] }],
  },
];
async function openForm(page: Page, selected = false, cases = sampleCases, mode: 'bank' | 'external' | 'integrated' = 'bank') {
  const requests: { path: string; method: string; body: any; headers: Record<string, string> }[] =
    [];
  let createdTask: Record<string, unknown> | undefined;
  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (!path.startsWith('/api/')) return route.fallback();
    requests.push({
      path,
      method: req.method(),
      body: req.postDataJSON(),
      headers: await req.allHeaders(),
    });
    if (path === '/api/datasets')
      return reply(route, [
        { id: 'dataset', name: '测试测评集', version: 2 },
        { id: 'other', name: '另一测评集', version: 1 },
      ]);
    if (path === '/api/evaluators')
      return reply(route, [
        {
          id: 'judge',
          name: '质量评估',
          kind: 'llm_judge',
          enabled: true,
          latest_version: '1',
          implementation_id: 'answer_quality',
          description: '质量',
          source: 'custom',
        },
      ]);
    if (path.match(/\/datasets\/[^/]+$/))
      return reply(route, {
        versions: [
          { version: 1, cases },
          { version: 2, cases },
        ],
      });
    if (path.includes('/datasets/')) return reply(route, { cases });
    if (path === '/api/versions')
      return reply(route, [
        { id: 'old-1', label: '旧版一' },
        { id: 'old-2', label: '旧版二' },
      ]);
    if (path === '/api/bank-targets' || path === '/api/evaluation-tasks') return reply(route, []);
    if (path === '/api/local-targets') return reply(route, { targets: [], unavailable: [] });
    if (path === '/api/agent-platform/evaluations') {
      const count = req.postDataJSON().repetitions;
      createdTask = {
        id: 'task-1', name: req.postDataJSON().name,
        kind: count > 1 ? 'stability' : 'single',
        run_ids: Array.from({ length: count }, (_, i) => 'run-' + i),
        static_report_ids: [],
      };
      return reply(
        route,
        wrap({
          id: 'task-1',
          kind: count > 1 ? 'stability' : 'single',
          run_ids: Array.from({ length: count }, (_, i) => 'run-' + i),
        }),
      );
    }
    if (path === '/api/agent-platform/comparisons') {
      createdTask = {
        id: 'ab-a', name: req.postDataJSON().name, kind: 'ab',
        run_ids: ['ab-a', 'ab-b'], static_report_ids: [],
      };
      return reply(route, {
        baseline: { run_id: 'ab-a', status: 'pending' },
        candidate: { run_id: 'ab-b', status: 'pending' },
      });
    }
    if (createdTask && path === '/api/evaluation-tasks/' + createdTask.id)
      return reply(route, createdTask);
    if (path === '/api/run-comparisons')
      return reply(route, { baseline: { run_id: 'a' }, candidate: { run_id: 'b' } });
    if (path === '/api/evaluation-tasks/a') return reply(route, {});
    return reply(route, { detail: 'unexpected request' }, 500);
  });
  await page.route('**/web/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    return reply(
      route,
      path.endsWith('getTeamRole')
        ? wrap(batch([{ teamId: 'team', teamName: '测试团队' }]))
        : path.endsWith('/agents')
          ? batch([
              { id: 'workflow', name: '工作流', agentType: 'workflow' },
              { id: 'base', name: '基础编排', agentType: 'base' },
              { id: 'claw', name: '云虾', agentType: 'abcclaw2' },
            ])
          : path.endsWith('branchTree')
            ? wrap([{ branchId: 'branch/raw', branchName: '分支' }])
            : path.endsWith('listVersions')
              ? wrap([
                  { agentVersion: 'v2', branchId: 'branch/raw' },
                  { agentVersion: 'v3', branchId: 'branch/raw' },
                ])
              : wrap([
                  { agentVersion: 'v1', updatedAt: bankVersionOne },
                  { agentVersion: 'v2', updatedAt: bankVersionTwo },
                ]),
    );
  });
  const query = new URLSearchParams({ mode, ...(selected ? { selected: 'true' } : {}) });
  await page.goto(url.replace('__directory', '__form') + '?' + query);
  await expect(page.getByLabel('任务测评集版本', { exact: true })).toHaveValue('2');
  await expect(page.getByRole('combobox', { name: '选择智能体', exact: true })).toBeEnabled();
  await page.getByLabel('任务名称', { exact: true }).fill('版本固定测试');
  if (mode === 'integrated')
    expect(await page.evaluate(() => (window as any).readAuth())).toEqual({
      loginMode: 'integrated', platformReady: true, usesPlatformDirectory: true,
      userId: 'integrated-user', teamName: '集成测试团队',
    });
  return requests;
}
async function selectFormTarget(page: Page, claw = false, mode: 'bank' | 'integrated' = 'bank') {
  await page.evaluate(mode => {
    if (mode === 'integrated') (window as any).initializePlatform();
    else (window as any).setAuth({
      loginMode: 'bank', token: 'form-secret', teamId: 'team', teamName: '测试团队',
    });
  }, mode);
  await choose(page, '选择智能体', claw ? '云虾 · claw' : '工作流 · workflow');
  if (claw) await choose(page, '分支地址', '分支 · branch/raw');
  await choose(page, '智能体版本', claw ? 'v2' : bankVersionOne);
  if (mode === 'integrated')
    expect(await page.evaluate(() => (window as any).readAuth())).toMatchObject({
      loginMode: 'integrated', platformReady: true,
    });
}
async function useExternalMode(page: Page) {
  await page.route('**/api/local-targets', (route) => reply(route, { targets: [], unavailable: [] }));
  await page.evaluate(() =>
    (window as any).setAuth({ loginMode: 'external', token: '', teamId: '', teamName: '' }),
  );
}
const start = (page: Page) => page.getByRole('button', { name: '开始测评', exact: true });

for (const mode of ['bank', 'external'] as const)
  for (const kind of ['single', 'ab'] as const)
    test(`${mode} platform ${kind} allows editing timeout after agent and dataset selection`, async ({ page }) => {
      const requests = await openForm(page, false, sampleCases, mode);
      await choose(page, '选择智能体', '工作流 · workflow');
      await choose(page, '智能体版本', mode === 'bank' ? bankVersionOne : 'v1');
      if (kind === 'ab') {
        await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
        await page.getByLabel('平台候选版本', { exact: true }).selectOption(
          mode === 'bank' ? bankVersionTwo : 'v2',
        );
      }
      await page.getByLabel('任务测评集', { exact: true }).selectOption('other');
      await expect(page.getByLabel('任务测评集版本', { exact: true })).toBeEnabled();
      await page.getByLabel('任务测评集版本', { exact: true }).selectOption('1');
      const timeoutInput = page.getByLabel('执行超时（秒）', { exact: true });
      await expect(timeoutInput).toBeEnabled();
      await expect(timeoutInput).toHaveValue('900');
      await timeoutInput.fill('1200');
      await timeoutInput.blur();
      await expect(timeoutInput).toHaveValue('1200');
      await page.getByRole('button', {
        name: kind === 'ab' ? '创建 A/B试验' : '开始测评', exact: true,
      }).click();
      await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
      const endpoint = kind === 'ab'
        ? '/api/agent-platform/comparisons' : '/api/agent-platform/evaluations';
      const submission = requests.find((request) => request.path === endpoint)!;
      expect(submission.body).toMatchObject({
        dataset_id: 'other', dataset_version: 1, timeout_seconds: 1200,
      });
      expect(submission.headers['x-agent-platform-token']).toBe(mode === 'bank' ? 'form-secret' : 'local');
    });

for (const mode of ['bank', 'external'] as const)
  test(`${mode} execution settings hide repetition and retry controls without empty layout cells`, async ({ page }) => {
    await openForm(page, false, sampleCases, mode);
    const settings = page.getByLabel('执行参数', { exact: true });
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      await expect(settings.locator(':scope > .field')).toHaveCount(3);
      await expect(settings.getByLabel('执行时间', { exact: true })).toBeVisible();
      await expect(settings.getByLabel('并发样本数', { exact: true })).toBeVisible();
      await expect(settings.getByLabel('执行超时（秒）', { exact: true })).toBeVisible();
      await expect(settings.getByText('稳定性测试', { exact: true })).toHaveCount(0);
      await expect(settings.getByText('失败重试次数', { exact: true })).toHaveCount(0);
    }
    await settings.getByLabel('执行时间', { exact: true }).selectOption('scheduled');
    await expect(settings.getByLabel('预约时间', { exact: true })).toBeVisible();
    await expect(settings.locator(':scope > .field')).toHaveCount(4);
    await settings.getByLabel('执行时间', { exact: true }).selectOption('now');
    await expect(settings.locator(':scope > .field')).toHaveCount(3);
  });

for (const mode of ['bank', 'integrated'] as const)
test(`${mode} workflow accepts business JSON while cloudshrimp retains its native input contract`, async ({ page }) => {
  const cases = [{ ...sampleCases[0], initial_state: { config_variables: [{ name: 'centreId', value: '810000' }] },
    turns: [{ input: { user_input_text: '翻译', menuId: 'TRANSLATE', form: { enabled: false } }, expectations: [{ kind: 'output' }] },
      { input: { user_input_text: '第二轮', lang_tar: 'fr_FR' }, expectations: [{ kind: 'output' }] }] }];
  const requests = await openForm(page, false, cases as typeof sampleCases, mode);
  await selectFormTarget(page, false, mode);
  await start(page).click();
  await expect.poll(() => requests.filter(r => r.path === '/api/agent-platform/evaluations').length).toBe(1);
  const sent = requests.find(r => r.path === '/api/agent-platform/evaluations')!;
  expect(sent.body.target).toEqual({
    team_id: 'team', agent_id: 'workflow', type_group: 'base/workflow',
    agent_version: bankVersionOne, arrange_type: 'workflow',
  });
  expect(sent.headers['x-agent-platform-token']).toBe(mode === 'integrated' ? 'integrated-secret' : 'form-secret');
  await selectFormTarget(page, true, mode);
  await start(page).click();
  await expect(page.getByText(/所选云虾.*输入格式不正确/)).toBeVisible();
  expect(requests.filter(r => r.path === '/api/agent-platform/evaluations')).toHaveLength(1);
});

test('invalid workflow config is rejected before task submission', async ({ page }) => {
  const cases = [{ ...sampleCases[0], initial_state: { config_variables: [{ name: 'centreId' }] } }];
  const requests = await openForm(page, false, cases as typeof sampleCases);
  await selectFormTarget(page);
  await start(page).click();
  await expect(page.getByText(/初始化变量或输入格式不正确/)).toBeVisible();
  expect(requests.some(r => r.path === '/api/agent-platform/evaluations')).toBe(false);
});

for (const mode of ['bank', 'integrated'] as const)
test(`${mode} base accepts both initialization arrays and retains initialization type isolation`, async ({ page }) => {
  const cases = [{ ...sampleCases[0], initial_state: {
    prompt_variables: [{ name: 'userId', value: 'hk_yat' }],
    tool_variables: [{ name: 'bankNo', value: '810000', type: 'str' }],
  } }];
  const requests = await openForm(page, false, cases as typeof sampleCases, mode);
  await choose(page, '选择智能体', '基础编排 · base');
  await choose(page, '智能体版本', bankVersionOne);
  await start(page).click();
  await expect.poll(() => requests.filter(r => r.path === '/api/agent-platform/evaluations').length).toBe(1);
  const sent = requests.find(r => r.path === '/api/agent-platform/evaluations')!;
  expect(sent.body.target).toEqual({
    team_id: 'team', agent_id: 'base', type_group: 'base/workflow',
    agent_version: bankVersionOne, arrange_type: 'base',
  });
  expect(sent.headers['x-agent-platform-token']).toBe(mode === 'integrated' ? 'integrated-secret' : 'form-secret');
  await selectFormTarget(page, false, mode);
  await start(page).click();
  await expect(page.getByText(/所选工作流.*初始化变量或输入格式不正确/)).toBeVisible();
  expect(requests.filter(r => r.path === '/api/agent-platform/evaluations')).toHaveLength(1);
});

for (const initial of [
  { config_variables: [{ name: 'centreId', value: '810000' }] },
  { config_variables: [], prompt_variables: [{ name: 'userId', value: 'hk_yat' }] },
  { prompt_variables: 'not-an-array' },
]) test('bank base rejects incompatible initialization ' + JSON.stringify(initial), async ({ page }) => {
  const requests = await openForm(page, false, [{ ...sampleCases[0], initial_state: initial }] as typeof sampleCases);
  await choose(page, '选择智能体', '基础编排 · base');
  await choose(page, '智能体版本', bankVersionOne);
  await start(page).click();
  await expect(page.getByText(/所选基础编排.*初始化变量或输入格式不正确/)).toBeVisible();
  expect(requests.some(r => r.path === '/api/agent-platform/evaluations')).toBe(false);
});

test('bank base accepts business JSON while cloudshrimp retains its native input contract', async ({ page }) => {
  const cases = [{ ...sampleCases[0], initial_state: { prompt_variables: [] },
    turns: [
      { input: { user_input_text: '业务 JSON', menuId: 'KNOWLEDGE_QUERY', form: { items: [0, false, null] } }, expectations: [{ kind: 'output' }] },
      { input: { txt: '普通文本' }, expectations: [{ kind: 'output' }] },
      { input: { user_input: '查询年休假政策' }, expectations: [{ kind: 'output' }] },
    ] }];
  const requests = await openForm(page, false, cases as unknown as typeof sampleCases);
  await choose(page, '选择智能体', '基础编排 · base');
  await choose(page, '智能体版本', bankVersionOne);
  await start(page).click();
  await expect.poll(() => requests.filter(r => r.path === '/api/agent-platform/evaluations').length).toBe(1);
  const sent = requests.find(r => r.path === '/api/agent-platform/evaluations')!;
  expect(sent.body.target.arrange_type).toBe('base');
  expect(sent.headers['x-agent-platform-token']).toBe('form-secret');
  await selectFormTarget(page, true);
  await start(page).click();
  await expect(page.getByText(/所选云虾.*输入格式不正确/)).toBeVisible();
  expect(requests.filter(r => r.path === '/api/agent-platform/evaluations')).toHaveLength(1);
});

test('external base still rejects business JSON before task submission', async ({ page }) => {
  const cases = [{ ...sampleCases[0], turns: [
    { input: { user_input_text: '业务 JSON' }, expectations: [{ kind: 'output' }] },
  ] }];
  const requests = await openForm(page, false, cases as unknown as typeof sampleCases);
  await page.route('**/api/local-targets', (route) => reply(route, { targets: [], unavailable: [] }));
  await page.evaluate(() =>
    (window as any).setAuth({ loginMode: 'external', token: '', teamId: '', teamName: '' }),
  );
  await choose(page, '选择智能体', '基础编排 · base');
  await choose(page, '智能体版本', 'v1');
  await start(page).click();
  await expect(page.getByText(/平台目标仅支持纯文本/)).toBeVisible();
  expect(requests.some(r => r.path === '/api/agent-platform/evaluations')).toBe(false);
});

for (const input of [
  { txt: '只填文本' },
  { txt: '{"user_input_text":"JSON 原文"}' },
  { txt: '历史', appHistory: [{ role: 'user', content: '之前的问题' }] },
  { txt: '客户', custID: 'hk_yat' },
  { txt: '变量', config_variables: [{ name: 'centreId', value: '810000' }] },
  { txt: '完整', appHistory: { role: 'user', content: '历史' }, custID: 'hk_yat',
    sessionId: 'session-one', config_variables: [{ open: [0, false, null] }] },
  { txt: '选填留空', appHistory: null, custID: null, sessionId: null, config_variables: null },
]) test('bank cloudshrimp accepts optional native input ' + JSON.stringify(input), async ({ page }) => {
  const cases = [{ ...sampleCases[0], turns: [
    { input, expectations: [{ kind: 'output' }] },
    { input: { txt: '第二轮仅文本' }, expectations: [{ kind: 'output' }] },
  ] }];
  const requests = await openForm(page, false, cases as unknown as typeof sampleCases);
  await selectFormTarget(page, true);
  await start(page).click();
  await expect.poll(() => requests.filter(r => r.path === '/api/agent-platform/evaluations').length).toBe(1);
  const sent = requests.find(r => r.path === '/api/agent-platform/evaluations')!;
  expect(sent.body.target.type_group).toBe('abcclaw');
  expect(sent.body.target.branch_id).toBe('branch/raw');
  expect(sent.headers['x-agent-platform-token']).toBe('form-secret');
});

test('integrated cloudshrimp submits native input with the platform session token', async ({ page }) => {
  const cases = [{ ...sampleCases[0], turns: [
    { input: { txt: '集成模式客户问题', appHistory: [{ role: 'user', content: '历史问题' }],
      custID: 'hk_yat', sessionId: 'integrated-session',
      config_variables: [{ name: 'centreId', value: '810000' }], files: [] },
      expectations: [{ kind: 'output' }] },
    { input: { txt: '第二轮仅文本', sessionId: 'integrated-session' }, expectations: [{ kind: 'output' }] },
  ] }];
  const requests = await openForm(page, false, cases as unknown as typeof sampleCases, 'integrated');
  await selectFormTarget(page, true, 'integrated');
  await start(page).click();
  await expect.poll(() => requests.filter(r => r.path === '/api/agent-platform/evaluations').length).toBe(1);
  const sent = requests.find(r => r.path === '/api/agent-platform/evaluations')!;
  expect(sent.body.target).toEqual({
    team_id: 'team', agent_id: 'claw', type_group: 'abcclaw',
    agent_version: 'v2', branch_id: 'branch/raw',
  });
  expect(sent.headers['x-agent-platform-token']).toBe('integrated-secret');
  expect(requests.some(r => r.path === '/api/run-comparisons')).toBe(false);
});

for (const inputs of [
  [{ appHistory: [] }], [{ txt: ' ' }], [{ txt: 1 }], [{ txt: 'hello', app_history: [] }],
  [{ txt: 'hello', custID: 123 }], [{ txt: 'hello', sessionId: [] }],
  [{ txt: 'hello', config_variables: {} }], [{ txt: 'hello', files: ['unsupported'] }],
  [{ txt: 'one', sessionId: 'one' }, { txt: 'two', sessionId: 'two' }],
]) test('bank cloudshrimp rejects invalid native inputs ' + JSON.stringify(inputs), async ({ page }) => {
  const cases = [{ ...sampleCases[0], turns: inputs.map(input => ({ input, expectations: [{ kind: 'output' }] })) }];
  const requests = await openForm(page, false, cases as unknown as typeof sampleCases);
  await selectFormTarget(page, true);
  await start(page).click();
  await expect(page.getByText(/所选云虾.*输入格式不正确/)).toBeVisible();
  expect(requests.some(r => r.path === '/api/agent-platform/evaluations')).toBe(false);
});

test('bank cloudshrimp keeps request fields in input rather than Case initialization', async ({ page }) => {
  const cases = [{ ...sampleCases[0], initial_state: { config_variables: [] } }];
  const requests = await openForm(page, false, cases as typeof sampleCases);
  await selectFormTarget(page, true);
  await start(page).click();
  await expect(page.getByText(/所选云虾.*输入格式不正确/)).toBeVisible();
  expect(requests.some(r => r.path === '/api/agent-platform/evaluations')).toBe(false);
});

test('external cloudshrimp does not adopt inbank native request fields', async ({ page }) => {
  const cases = [{ ...sampleCases[0], turns: [
    { input: { txt: 'hello', appHistory: [] }, expectations: [{ kind: 'output' }] },
  ] }];
  const requests = await openForm(page, false, cases as unknown as typeof sampleCases);
  await page.route('**/api/local-targets', (route) => reply(route, { targets: [], unavailable: [] }));
  await page.evaluate(() =>
    (window as any).setAuth({ loginMode: 'external', token: '', teamId: '', teamName: '' }),
  );
  await choose(page, '选择智能体', '云虾 · claw');
  await choose(page, '分支地址', '分支 · branch/raw');
  await choose(page, '智能体版本', 'v2');
  await start(page).click();
  await expect(page.getByText(/平台目标仅支持纯文本/)).toBeVisible();
  expect(requests.some(r => r.path === '/api/agent-platform/evaluations')).toBe(false);
});

for (const input of [{}, { txt: ' ' }, { txt: 'hello', files: ['unsupported'] }])
  test('bank base rejects invalid text and attachments ' + JSON.stringify(input), async ({ page }) => {
    const cases = [{ ...sampleCases[0], turns: [{ input, expectations: [{ kind: 'output' }] }] }];
    const requests = await openForm(page, false, cases as unknown as typeof sampleCases);
    await choose(page, '选择智能体', '基础编排 · base');
    await choose(page, '智能体版本', bankVersionOne);
    await start(page).click();
    await expect(page.getByText(/所选基础编排.*初始化变量或输入格式不正确/)).toBeVisible();
    expect(requests.some(r => r.path === '/api/agent-platform/evaluations')).toBe(false);
  });

test('form preserves dataset/execution choices through target changes and logout, without legacy catalogs', async ({
  page,
}) => {
  const requests = await openForm(page);
  await page.getByLabel('任务测评集', { exact: true }).selectOption('other');
  await page.getByLabel('并发样本数', { exact: true }).fill('7');
  await page.getByLabel('执行超时（秒）', { exact: true }).fill('999');
  await selectFormTarget(page);
  await choose(page, '智能体版本', bankVersionTwo);
  await expect(page.getByLabel('任务测评集', { exact: true })).toHaveValue('other');
  await expect(page.getByLabel('并发样本数', { exact: true })).toHaveValue('7');
  await expect(page.getByLabel('执行超时（秒）', { exact: true })).toHaveValue('999');
  await useExternalMode(page);
  await expect(page.getByLabel('并发样本数', { exact: true })).toHaveValue('7');
  await expect(page.getByRole('button', { name: 'Skill 静态分析', exact: true })).toHaveCount(0);
  await page.evaluate(() => (window as any).setAuth('logout'));
  await expect(start(page)).toBeDisabled();
  expect(requests.some((r) => ['/api/versions', '/api/bank-targets'].includes(r.path))).toBe(false);
});

test('in-bank single and A/B require a platform target without loading or showing demo fallback', async ({
  page,
}) => {
  const requests = await openForm(page);
  await expect(start(page)).toBeDisabled();
  await expect(page.locator('.target-choice')).toHaveCount(0);
  await expect(page.getByText('真实智能体目录暂不可用', { exact: false })).toHaveCount(0);
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  const create = page.getByRole('button', { name: '创建 A/B试验', exact: true });
  await expect(create).toBeDisabled();
  await expect(page.locator('.target-choice')).toHaveCount(0);
  await expect(page.getByText('内置 Demo', { exact: false })).toHaveCount(0);
  await expect(page.locator('.task-summary')).toContainText('待选目标');
  await create.dispatchEvent('click');
  await expect(
    page.getByRole('alert').filter({ hasText: '请完整选择被测智能体及版本' }),
  ).toBeVisible();
  expect(
    requests.some((r) =>
      ['/api/versions', '/api/bank-targets', '/api/run-comparisons'].includes(r.path),
    ),
  ).toBe(false);
  expect(await page.evaluate(() => (window as any).created)).toEqual([]);
});

test('switching external A/B to in-bank discards late demo catalogs and requires a platform target', async ({
  page,
}) => {
  const requests = await openForm(page, false, sampleCases, 'external');
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  let pending = 0;
  await page.route('**/api/versions', async (route) => {
    pending++;
    await gate;
    await reply(route, [
      { id: 'late-1', label: '迟到版本一' },
      { id: 'late-2', label: '迟到版本二' },
    ]);
  });
  await page.route('**/api/bank-targets', async (route) => {
    pending++;
    await gate;
    await reply(route, { detail: 'unavailable' }, 503);
  });
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  await expect.poll(() => pending).toBe(2);
  await page.evaluate(() =>
    (window as any).setAuth({
      loginMode: 'bank',
      token: 'form-secret',
      teamId: 'team',
      teamName: '测试团队',
    }),
  );
  await expect(page.getByRole('combobox', { name: '选择智能体', exact: true })).toBeEnabled();
  const responses = Promise.all([
    page.waitForResponse('**/api/versions'),
    page.waitForResponse('**/api/bank-targets'),
  ]);
  release();
  await responses;
  const create = page.getByRole('button', { name: '创建 A/B试验', exact: true });
  await expect(create).toBeDisabled();
  await expect(page.locator('.target-choice')).toHaveCount(0);
  await expect(page.getByText('内置 Demo', { exact: false })).toHaveCount(0);
  await expect(page.getByText('正在加载 A/B 智能体目录…', { exact: true })).toHaveCount(0);
  await expect(page.locator('.task-summary')).toContainText('待选目标');
  await create.dispatchEvent('click');
  await expect(
    page.getByRole('alert').filter({ hasText: '请完整选择被测智能体及版本' }),
  ).toBeVisible();
  expect(requests.some((r) => r.path === '/api/run-comparisons')).toBe(false);
  await choose(page, '选择智能体', '工作流 · workflow');
  await choose(page, '实验 A · 基线版本', bankVersionOne);
  await page.getByLabel('平台候选版本', { exact: true }).selectOption(bankVersionTwo);
  await create.click();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  expect(
    requests.find((r) => r.path === '/api/agent-platform/comparisons')?.headers[
      'x-agent-platform-token'
    ],
  ).toBe('form-secret');
  expect(requests.some((r) => r.path === '/api/run-comparisons')).toBe(false);
});

test('form submits workflow target and selected source cases with isolated token and no association PUT', async ({
  page,
}) => {
  const requests = await openForm(page, true);
  await selectFormTarget(page);
  await start(page).click();
  await expect
    .poll(() => page.evaluate(() => (window as any).created))
    .toEqual([{ id: 'task-1', kind: 'single', runIds: ['run-0'], staticReports: [] }]);
  const submission = requests.find((r) => r.path === '/api/agent-platform/evaluations')!;
  expect(submission.body).toEqual({
    name: '版本固定测试',
    target: {
      team_id: 'team',
      agent_id: 'workflow',
      type_group: 'base/workflow',
      agent_version: '2026-05-06 17:21:16',
      arrange_type: 'workflow',
    },
    dataset_id: 'dataset',
    dataset_version: 2,
    case_ids: ['case-1'],
    evaluator_refs: [{ evaluator_id: 'judge', evaluator_version: '1' }],
    max_parallel_cases: 1,
    timeout_seconds: 900,
    max_retries: 0,
    repetitions: 1,
  });
  expect(submission.headers['x-agent-platform-token']).toBe('form-secret');
  expect(
    requests.filter((r) => r !== submission).some((r) => r.headers['x-agent-platform-token']),
  ).toBe(false);
  expect(requests.some((r) => r.method === 'PUT')).toBe(false);
  expect(
    await page.evaluate(() => ({
      updates: (window as any).updates,
      storage: JSON.stringify({ ...localStorage, ...sessionStorage }),
    })),
  ).toEqual({ updates: 1, storage: '{}' });
});

test('platform A/B submits both versions to the comparison endpoint', async ({
  page,
}, testInfo) => {
  const requests = await openForm(page);
  await selectFormTarget(page);
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  const candidate = page.getByLabel('平台候选版本', { exact: true });
  await expect(candidate).toBeEnabled();
  await candidate.selectOption(bankVersionTwo);
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page
    .locator('.task-create-dialog')
    .screenshot({ path: testInfo.outputPath('inbank-ab-desktop.png') });
  await page.setViewportSize({ width: 600, height: 1100 });
  await page
    .locator('.task-create-dialog')
    .screenshot({ path: testInfo.outputPath('inbank-ab-narrow.png') });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.getByRole('button', { name: '创建 A/B试验', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  const submission = requests.find((r) => r.path === '/api/agent-platform/comparisons')!;
  expect(submission.body).toEqual({
    name: '版本固定测试',
    target: {
      team_id: 'team',
      agent_id: 'workflow',
      type_group: 'base/workflow',
      baseline_version: '2026-05-06 17:21:16',
      candidate_version: '2026-05-06 17:20:34',
      arrange_type: 'workflow',
    },
    dataset_id: 'dataset',
    dataset_version: 2,
    evaluator_refs: [{ evaluator_id: 'judge', evaluator_version: '1' }],
    max_parallel_cases: 1,
    timeout_seconds: 900,
  });
  expect(submission.headers['x-agent-platform-token']).toBe('form-secret');
  expect(requests.some((r) => r.path === '/api/run-comparisons')).toBe(false);
  expect(requests.some((r) => ['/api/versions', '/api/bank-targets'].includes(r.path))).toBe(false);
  await expect(page.locator('.task-summary')).toContainText('工作流');
});

for (const mode of ['bank', 'external'] as const)
  for (const kind of ['single', 'ab'] as const)
    test(`${mode} ${kind} submission waits only for the newly created task`, async ({ page }) => {
      const requests = await openForm(page);
      if (mode === 'bank') await selectFormTarget(page);
      else {
        await useExternalMode(page);
        await choose(page, '选择智能体', '工作流 · workflow');
        await choose(page, '智能体版本', 'v1');
      }
      if (kind === 'ab') {
        await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
        await page.getByLabel('平台候选版本', { exact: true })
          .selectOption(mode === 'bank' ? bankVersionTwo : 'v2');
      }
      let bulkReads = 0;
      await page.route('**/api/evaluation-tasks', (route) => {
        bulkReads++;
        return reply(route, { detail: 'Historical tasks are unavailable' }, 503);
      });
      const taskId = kind === 'ab' ? 'ab-a' : 'task-1';
      let taskReads = 0;
      let release!: () => void;
      const gate = new Promise<void>((resolve) => (release = resolve));
      await page.route('**/api/evaluation-tasks/' + taskId, async (route) => {
        taskReads++;
        await gate;
        await route.fallback();
      });
      await page.getByRole('button', {
        name: kind === 'ab' ? '创建 A/B试验' : '开始测评', exact: true,
      }).click();
      try {
        await expect.poll(() => taskReads).toBe(1);
        expect(await page.evaluate(() => (window as any).created)).toEqual([]);
        await expect(page.getByRole('button', { name: '正在处理…', exact: true })).toBeDisabled();
      } finally {
        release();
      }
      await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
      const runIds = kind === 'ab' ? ['ab-a', 'ab-b'] : ['run-0'];
      expect(await page.evaluate(() => (window as any).readTaskLinks())).toEqual([
        expect.objectContaining({ id: taskId, name: '版本固定测试', kind, runIds, staticReports: [] }),
      ]);
      await expect(page.getByText(kind === 'ab' ? 'A/B试验已提交' : '测评任务已提交', { exact: true }))
        .toBeVisible();
      expect(bulkReads).toBe(0);
      expect(requests.filter((request) => request.method === 'POST')).toHaveLength(1);
      expect(requests.some((request) => request.path.startsWith('/api/skill-analysis/reports/'))).toBe(false);
      const read = requests.find((request) => request.path === '/api/evaluation-tasks/' + taskId)!;
      expect(read.headers['x-agent-platform-token']).toBeUndefined();
      expect(read.headers.token).toBe(mode === 'bank' ? 'form-secret' : undefined);
      if (mode === 'bank') expect(read.headers['x-team-id']).toBe('team');
    });

test('A/B task refresh failure still emits creation without submitting twice', async ({ page }) => {
  const requests = await openForm(page);
  await selectFormTarget(page);
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  await page.getByLabel('平台候选版本', { exact: true }).selectOption(bankVersionTwo);
  await page.route('**/api/evaluation-tasks/ab-a', (route) =>
    reply(route, { detail: 'form-secret' }, 500),
  );
  await page.getByRole('button', { name: '创建 A/B试验', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).created)).toEqual([
    { id: 'ab-a', kind: 'ab', runIds: ['ab-a', 'ab-b'], staticReports: [] },
  ]);
  await expect(page.getByText('实验已创建（ab-a），但列表刷新失败，请刷新任务列表；请勿重复提交。'))
    .toBeVisible();
  expect(requests.filter((request) => request.method === 'POST')).toHaveLength(1);
  expect(await page.locator('body').innerText()).not.toContain('form-secret');
});

test('platform A/B requires a different candidate version before creating', async ({ page }) => {
  await openForm(page);
  await selectFormTarget(page);
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  const create = page.getByRole('button', { name: '创建 A/B试验', exact: true });
  await expect(create).toBeDisabled();
  const candidate = page.getByLabel('平台候选版本', { exact: true });
  await expect(candidate).toBeEnabled();
  await candidate.selectOption(bankVersionOne);
  await expect(create).toBeDisabled();
  await candidate.selectOption(bankVersionTwo);
  await expect(create).toBeEnabled();
});

for (const [mode, target, kind] of [
  ['bank', 'workflow', 'single'], ['bank', 'workflow', 'ab'],
  ['bank', 'base', 'single'], ['bank', 'base', 'ab'],
  ['bank', 'claw', 'single'], ['bank', 'claw', 'ab'],
  ['integrated', 'workflow', 'ab'],
] as const)
    test(`${mode} ${target} ${kind} submits the editable Case concurrency`, async ({ page }) => {
      const requests = await openForm(page, false, sampleCases, mode);
      await page.getByLabel('并发样本数', { exact: true }).fill('4');
      const directoryRequests: { origin: string; authorization?: string }[] = [];
      page.on('request', (request) => {
        const location = new URL(request.url());
        if (location.pathname.startsWith('/web/'))
          directoryRequests.push({
            origin: location.origin,
            authorization: request.headers().authorization,
          });
      });
      await choose(page, '选择智能体', {
        workflow: '工作流 · workflow', base: '基础编排 · base', claw: '云虾 · claw',
      }[target]);
      if (target === 'claw') await choose(page, '分支地址', '分支 · branch/raw');
      await choose(page, '智能体版本', target === 'claw' ? 'v2' : bankVersionOne);
      const concurrencyInput = page.getByLabel('并发样本数', { exact: true });
      await expect(concurrencyInput).toBeEnabled();
      await expect(concurrencyInput).toHaveValue('4');
      if (kind === 'ab') {
        await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
        await page.getByLabel('平台候选版本', { exact: true })
          .selectOption(target === 'claw' ? 'v3' : bankVersionTwo);
        await expect(page.getByText('A/B 两侧使用相同并发数，失败重试 0。')).toBeVisible();
        await expect(concurrencyInput).toBeEnabled();
        await expect(concurrencyInput).toHaveValue('4');
        await concurrencyInput.fill('5');
      }
      await expect(page.getByLabel('稳定性测试', { exact: true })).toHaveCount(0);
      await expect(page.getByLabel('失败重试次数', { exact: true })).toHaveCount(0);
      await page.getByRole('button', {
        name: kind === 'ab' ? '创建 A/B试验' : '开始测评', exact: true,
      }).click();
      await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
      const endpoint = kind === 'ab'
        ? '/api/agent-platform/comparisons' : '/api/agent-platform/evaluations';
      const submission = requests.find((request) => request.path === endpoint)!;
      expect(submission.body.max_parallel_cases).toBe(kind === 'ab' ? 5 : 4);
      const token = mode === 'integrated' ? 'integrated-secret' : 'form-secret';
      expect(submission.headers['x-agent-platform-token']).toBe(token);
      expect(submission.body.target).toEqual({
        team_id: 'team', agent_id: target,
        type_group: target === 'claw' ? 'abcclaw' : 'base/workflow',
        ...(target === 'claw' ? { branch_id: 'branch/raw' } : { arrange_type: target }),
        ...(kind === 'ab'
          ? { baseline_version: target === 'claw' ? 'v2' : bankVersionOne,
              candidate_version: target === 'claw' ? 'v3' : bankVersionTwo }
          : { agent_version: target === 'claw' ? 'v2' : bankVersionOne }),
      });
      expect(directoryRequests.length).toBeGreaterThan(0);
      expect(directoryRequests.every((request) =>
        request.origin === new URL(url).origin && request.authorization === `Bearer ${token}`,
      )).toBe(true);
      if (kind !== 'ab') {
        expect(submission.body.repetitions).toBe(1);
        expect(submission.body.max_retries).toBe(0);
      }
    });

for (const invalid of ['0', '31', '1.5'])
  test(`bank A/B retains the existing Case concurrency bounds (${invalid})`, async ({ page }) => {
    const requests = await openForm(page);
    await selectFormTarget(page);
    await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
    await page.getByLabel('平台候选版本', { exact: true }).selectOption(bankVersionTwo);
    await page.getByLabel('并发样本数', { exact: true }).fill(invalid);
    await page.getByRole('button', { name: '创建 A/B试验', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: '并发样本数必须为 1—30 的整数' }))
      .toBeVisible();
    expect(requests.some((request) => request.path === '/api/agent-platform/comparisons')).toBe(false);
  });

test('switching from bank A/B to external restores its fixed concurrency', async ({ page }) => {
  const requests = await openForm(page);
  await selectFormTarget(page);
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  const concurrencyInput = page.getByLabel('并发样本数', { exact: true });
  await concurrencyInput.fill('5');
  await useExternalMode(page);
  await choose(page, '选择智能体', '工作流 · workflow');
  await choose(page, '实验 A · 基线版本', 'v1');
  await page.getByLabel('平台候选版本', { exact: true }).selectOption('v2');
  await expect(concurrencyInput).toHaveValue('1');
  await expect(concurrencyInput).toBeDisabled();
  await page.getByRole('button', { name: '创建 A/B试验', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  const submission = requests.find((request) => request.path === '/api/agent-platform/comparisons')!;
  expect(submission.body).not.toHaveProperty('max_parallel_cases');
  expect(submission.headers['x-agent-platform-token']).toBe('local');
});

for (const kind of ['single', 'ab'] as const)
  test(`external platform ${kind} retains its existing concurrency and token routing`, async ({ page }) => {
    const requests = await openForm(page);
    await useExternalMode(page);
    const directoryRequests: { origin: string; authorization?: string }[] = [];
    page.on('request', (request) => {
      const location = new URL(request.url());
      if (location.pathname.startsWith('/web/'))
        directoryRequests.push({ origin: location.origin, authorization: request.headers().authorization });
    });
    await choose(page, '选择智能体', '工作流 · workflow');
    await choose(page, '智能体版本', 'v1');
    const concurrencyInput = page.getByLabel('并发样本数', { exact: true });
    await expect(concurrencyInput).toBeVisible();
    await concurrencyInput.fill('4');
    if (kind === 'ab') {
      await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
      await page.getByLabel('平台候选版本', { exact: true }).selectOption('v2');
      await expect(concurrencyInput).toHaveValue('1');
      await expect(concurrencyInput).toBeDisabled();
      await expect(page.getByText('A/B 两侧沿用固定执行参数：并发 1，失败重试 0。')).toBeVisible();
    }
    await expect(page.getByLabel('稳定性测试', { exact: true })).toHaveCount(0);
    await expect(page.getByLabel('失败重试次数', { exact: true })).toHaveCount(0);
    await page.getByRole('button', {
      name: kind === 'ab' ? '创建 A/B试验' : '开始测评', exact: true,
    }).click();
    await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
    const endpoint = kind === 'ab'
      ? '/api/agent-platform/comparisons' : '/api/agent-platform/evaluations';
    const submission = requests.find((request) => request.path === endpoint)!;
    if (kind === 'ab') expect(submission.body).not.toHaveProperty('max_parallel_cases');
    else {
      expect(submission.body.max_parallel_cases).toBe(4);
      expect(submission.body.repetitions).toBe(1);
      expect(submission.body.max_retries).toBe(0);
    }
    expect(submission.headers['x-agent-platform-token']).toBe('local');
    expect(directoryRequests.length).toBeGreaterThan(0);
    expect(directoryRequests.every((request) =>
      request.origin === 'http://127.0.0.1:8119' && request.authorization === 'Bearer local',
    )).toBe(true);
  });

test('personal space submits without a team_id field', async ({ page }) => {
  const requests = await openForm(page);
  await page.evaluate(() =>
    (window as any).setAuth({
      loginMode: 'bank',
      token: 'form-secret',
      teamId: '',
      teamName: '个人空间',
    }),
  );
  await choose(page, '选择智能体', '工作流 · workflow');
  await choose(page, '智能体版本', bankVersionOne);
  await start(page).click();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  const submission = requests.find((r) => r.path === '/api/agent-platform/evaluations')!;
  expect(submission.body.target).toEqual({
    agent_id: 'workflow',
    type_group: 'base/workflow',
    agent_version: '2026-05-06 17:21:16',
    arrange_type: 'workflow',
  });
  expect('team_id' in submission.body.target).toBe(false);
});

test('abcclaw single sends original branchId and keeps hidden execution defaults', async ({ page }) => {
  const requests = await openForm(page);
  await selectFormTarget(page, true);
  await page.getByLabel('并发样本数', { exact: true }).fill('4');
  await page.getByLabel('执行超时（秒）', { exact: true }).fill('1200');
  await expect(page.getByLabel('稳定性测试', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('失败重试次数', { exact: true })).toHaveCount(0);
  await start(page).click();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  expect(requests.find((r) => r.path === '/api/agent-platform/evaluations')?.body).toEqual({
    name: '版本固定测试',
    target: {
      team_id: 'team',
      agent_id: 'claw',
      type_group: 'abcclaw',
      agent_version: 'v2',
      branch_id: 'branch/raw',
    },
    dataset_id: 'dataset',
    dataset_version: 2,
    evaluator_refs: [{ evaluator_id: 'judge', evaluator_version: '1' }],
    max_parallel_cases: 4,
    timeout_seconds: 1200,
    max_retries: 0,
    repetitions: 1,
  });
});

test('submission locks all inputs before asynchronous validation and prevents duplicate creation', async ({
  page,
}) => {
  const requests = await openForm(page, true);
  await selectFormTarget(page);
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  await page.route('**/api/datasets/dataset/versions/2', async (route) => {
    await gate;
    await reply(route, { cases: sampleCases });
  });
  await start(page).click();
  await expect(page.getByRole('button', { name: 'A/B试验', exact: true })).toBeDisabled();
  await expect(page.getByRole('combobox', { name: '选择智能体', exact: true })).toBeDisabled();
  await expect(page.getByLabel('并发样本数', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('执行超时（秒）', { exact: true })).toBeDisabled();
  await page.locator('.dataset-selection .el-select').click({ force: true });
  await expect(page.getByRole('listbox', { name: '指定用例', exact: true })).toBeHidden();
  await expect(page.getByRole('button', { name: '取消', exact: true })).toBeDisabled();
  await page
    .getByRole('button', { name: '正在处理…', exact: true })
    .evaluate((button: HTMLButtonElement) => button.click());
  release();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  expect(requests.filter((r) => r.path === '/api/agent-platform/evaluations')).toHaveLength(1);
});

for (const failure of ['reject', 'server', 'malformed', 'refresh'] as const)
  test(`form safely handles ${failure} without leaking server detail or retrying`, async ({
    page,
  }) => {
    const requests = await openForm(page);
    await selectFormTarget(page);
    let posts = 0;
    if (failure === 'refresh')
      await page.route('**/api/evaluation-tasks/task-1', (route) =>
        reply(route, { detail: 'form-secret' }, 500),
      );
    else
      await page.route('**/api/agent-platform/evaluations', (route) => {
        posts++;
        return reply(
          route,
          failure === 'malformed'
            ? { id: 'task-1', kind: 'single', run_ids: [] }
            : { detail: 'form-secret' },
          failure === 'reject' ? 403 : failure === 'server' ? 500 : 200,
        );
      });
    await start(page).click();
    if (failure === 'refresh') {
      await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
      await expect(
        page.getByText('任务已创建（task-1），但列表刷新失败，请刷新任务列表；请勿重复提交。'),
      ).toBeVisible();
      expect(requests.filter((r) => r.path === '/api/agent-platform/evaluations')).toHaveLength(1);
    } else {
      await expect(
        page.getByRole('alert').filter({
          hasText: failure === 'reject' ? '任务提交被拒绝' : '暂时无法确认任务是否创建成功',
        }),
      ).toBeVisible();
      expect(posts).toBe(1);
      expect(await page.evaluate(() => (window as any).created)).toEqual([]);
    }
    expect(await page.locator('body').innerText()).not.toContain('form-secret');
  });

test('external A/B keeps legacy endpoint and association, returning to single requires fresh target selection', async ({
  page,
}) => {
  const requests = await openForm(page, false, sampleCases, 'external');
  // 不预选平台目标：内置 Demo 的 A/B 走 legacy 端点；预选平台目标会切换到平台 A/B 分支。
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  await expect(page.getByLabel('执行超时（秒）', { exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '创建 A/B试验', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Skill 静态分析', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('并发样本数', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('稳定性测试', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('失败重试次数', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '创建 A/B试验', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  expect(requests.find((r) => r.path === '/api/run-comparisons')?.body).toEqual({
    baseline_version: 'old-1',
    candidate_version: 'old-2',
    dataset_id: 'dataset',
    dataset_version: 2,
    evaluators: [{ id: 'judge', version: '1' }],
  });
  expect(requests.some((r) => r.method === 'PUT' && r.path === '/api/evaluation-tasks/a')).toBe(
    true,
  );
  await page.getByRole('button', { name: '单任务', exact: true }).click();
  await expect(page.getByRole('combobox', { name: '选择智能体', exact: true })).toBeEnabled();
  await expect(start(page)).toBeDisabled();
});

test('late external A/B catalogs cannot change the single task dataset or execution settings', async ({
  page,
}) => {
  await openForm(page, false, sampleCases, 'external');
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  let requested = false;
  await page.route('**/api/versions', async (route) => {
    requested = true;
    await gate;
    await reply(route, [{ id: 'late', label: '迟到版本' }]);
  });
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await page.getByRole('button', { name: '单任务', exact: true }).click();
  await page.getByLabel('并发样本数', { exact: true }).fill('9');
  release();
  await expect(page.getByRole('combobox', { name: '选择智能体', exact: true })).toBeEnabled();
  await choose(page, '选择智能体', '工作流 · workflow');
  await choose(page, '智能体版本', 'v1');
  await expect(page.getByLabel('并发样本数', { exact: true })).toHaveValue('9');
  await useExternalMode(page);
  await expect(page.getByLabel('并发样本数', { exact: true })).toHaveValue('9');
  await expect(page.getByLabel('任务测评集', { exact: true })).toHaveValue('dataset');
});

test('external scheduled execution sends UTC and rejects invalid numeric limits before creating', async ({
  page,
}) => {
  const requests = await openForm(page);
  await useExternalMode(page);
  await choose(page, '选择智能体', '工作流 · workflow');
  await choose(page, '智能体版本', 'v1');
  await page.getByLabel('执行时间', { exact: true }).selectOption('scheduled');
  const date = new Date(Date.now() + 86400000);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
  await page.getByLabel('预约时间', { exact: true }).fill(local);
  await page.getByLabel('并发样本数', { exact: true }).fill('31');
  await start(page).click();
  await expect(page.getByRole('alert').filter({ hasText: '并发样本数必须为' })).toBeVisible();
  expect(requests.some((r) => r.path === '/api/agent-platform/evaluations')).toBe(false);
  await page.getByLabel('并发样本数', { exact: true }).fill('3');
  await start(page).click();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  expect(
    requests.find((r) => r.path === '/api/agent-platform/evaluations')?.body.scheduled_for,
  ).toBe(new Date(local).toISOString());
});

test('cancelling uncovered-case confirmation releases the lock without a creation request', async ({
  page,
}) => {
  const requests = await openForm(page);
  await page.route('**/api/evaluators?*', (route) =>
    reply(route, [
      {
        id: 'judge',
        name: '输出检查',
        kind: 'rule',
        enabled: true,
        latest_version: '1',
        implementation_id: 'final_output',
        source: 'custom',
      },
    ]),
  );
  await page.reload();
  await expect(page.getByLabel('任务测评集版本', { exact: true })).toHaveValue('2');
  await page.getByLabel('任务名称', { exact: true }).fill('版本固定测试');
  await page.route('**/api/datasets/dataset/versions/2', (route) =>
    reply(route, {
      cases: [
        ...sampleCases,
        { id: 'uncovered', turns: [{ input: { txt: '未覆盖' }, expectations: [] }] },
      ],
    }),
  );
  await selectFormTarget(page);
  await start(page).click();
  await expect(page.getByText('1 条用例没有匹配检查，将标为不适用。是否继续？')).toBeVisible();
  await expect(page.getByRole('combobox', { name: '选择智能体', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '返回修改', exact: true }).click();
  await expect(start(page)).toBeEnabled();
  expect(requests.some((r) => r.path === '/api/agent-platform/evaluations')).toBe(false);
});

test('external A/B still permits explicit demo selection when the registered bank catalog fails', async ({
  page,
}) => {
  const requests = await openForm(page, false, sampleCases, 'external');
  await page.route('**/api/bank-targets', (route) => reply(route, { detail: 'unavailable' }, 503));
  await page.getByRole('button', { name: 'A/B试验', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: '真实智能体目录暂不可用' })).toBeVisible();
  await page.getByRole('button', { name: '创建 A/B试验', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
  expect(requests.some((r) => r.path === '/api/run-comparisons')).toBe(true);
});

for (const mode of ['bank', 'external'] as const)
  test(`form pins the displayed publication when a newer one appears (${mode})`, async ({
    page,
  }) => {
    const requests = await openForm(page);
    if (mode === 'external') {
      await page.route('**/api/local-targets', (route) =>
        reply(route, { targets: [], unavailable: [] }),
      );
      await page.evaluate(() =>
        (window as any).setAuth({ loginMode: 'external', token: '', teamId: '', teamName: '' }),
      );
    }
    await choose(page, '选择智能体', '工作流 · workflow');
    await choose(page, '智能体版本', mode === 'bank' ? bankVersionOne : 'v1');
    // Publish version 2 after the form loaded version 1. The submitted reference must stay at 1.
    await page.route('**/api/evaluators', (route) =>
      reply(route, [
        {
          id: 'judge',
          name: '质量评估',
          kind: 'llm_judge',
          enabled: true,
          latest_version: '2',
          implementation_id: 'answer_quality',
          description: '质量',
          source: 'custom',
        },
      ]),
    );
    await page.getByRole('button', { name: 'LLM 评估', exact: true }).click();
    await expect(page.locator('.tile-version')).toHaveText('v1');
    await start(page).click();
    await expect.poll(() => page.evaluate(() => (window as any).created.length)).toBe(1);
    const submission = requests.find((r) => r.path === '/api/agent-platform/evaluations')!;
    expect(submission.body.evaluator_refs).toEqual([
      { evaluator_id: 'judge', evaluator_version: '1' },
    ]);
    expect(submission.body).not.toHaveProperty('evaluator_ids');
    expect(submission.headers['x-agent-platform-token']).toBe(
      mode === 'bank' ? 'form-secret' : 'local',
    );
  });
