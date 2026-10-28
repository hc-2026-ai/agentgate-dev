import type {
  LocalTarget,
  AgentDirectory,
  AgentTypeGroup,
  PlatformAgent,
  PlatformBranch,
  PlatformCapability,
  PlatformInitializationVariable,
  PlatformTeam,
  PlatformVersion,
} from '../views/evaluation/components/AgentTargetPicker.vue';
import { request } from './evaluations';

export type AgentPlatformErrorKind =
  | 'invalid_input'
  | 'configuration'
  | 'http'
  | 'business'
  | 'protocol'
  | 'network'
  | 'timeout';
export type AgentPlatformError = Error & { status: number; kind: AgentPlatformErrorKind };

function failure(kind: AgentPlatformErrorKind, status = 0): AgentPlatformError {
  const messages: Record<AgentPlatformErrorKind, string> = {
    invalid_input: '目录查询参数无效。',
    configuration: '智能体平台地址配置无效。',
    http: '智能体平台请求失败。',
    business: '智能体平台未能完成查询。',
    protocol: '智能体平台返回的数据不符合接口约定。',
    network: '无法连接智能体平台。',
    timeout: '智能体平台查询超时，请重试。',
  };
  return Object.assign(new Error(messages[kind]), { status, kind });
}

function validateToken(token: string) {
  if (
    typeof token !== 'string' ||
    !token ||
    token.length > 512 ||
    /\s/.test(token) ||
    /^Bearer\b/i.test(token)
  ) {
    throw failure('invalid_input');
  }
}

function validateId(value: string) {
  if (typeof value !== 'string' || !value.trim()) throw failure('invalid_input');
}

function validatedOrigin(configured: unknown): string {
  if (
    typeof configured !== 'string' ||
    /\s/.test(configured) ||
    !/^https?:\/\/[^/?#\\]+\/?$/i.test(configured)
  )
    throw failure('configuration');
  let url: URL;
  try {
    url = new URL(configured);
  } catch {
    throw failure('configuration');
  }
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== '/' ||
    /[?#]/.test(configured)
  ) {
    throw failure('configuration');
  }
  return url.origin;
}

function endpoint(
  path: string,
  parameters: Record<string, string | number>,
  originOverride = '',
): string {
  let origin = '';
  if (originOverride) {
    origin = validatedOrigin(originOverride);
  } else {
    const configured: unknown = path.startsWith('/web/abcclaw/')
      ? import.meta.env.VITE_ABCCLAW_PLATFORM_ORIGIN
      : import.meta.env.VITE_AGENT_PLATFORM_ORIGIN;
    if (configured !== undefined && configured !== '') origin = validatedOrigin(configured);
  }
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(parameters)) query.set(key, String(value));
  return `${origin}${path}?${query}`;
}

async function query<T>(token: string, operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
  validateToken(token);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    return await operation(controller.signal);
  } finally {
    clearTimeout(timer);
  }
}

async function getJson(
  path: string,
  parameters: Record<string, string | number>,
  token: string,
  signal: AbortSignal,
  origin = '',
) {
  const url = endpoint(path, parameters, origin);
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json', Authorization: `Bearer ${token}` },
      credentials: 'omit',
      redirect: 'error',
      cache: 'no-store',
      signal,
    });
  } catch {
    throw failure(signal.aborted ? 'timeout' : 'network');
  }
  if (signal.aborted) throw failure('timeout');
  if (!response.ok) throw failure('http', response.status);
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw failure(signal.aborted ? 'timeout' : 'protocol', signal.aborted ? 0 : response.status);
  }
  if (signal.aborted) throw failure('timeout');
  return { value, status: response.status };
}

function object(value: unknown, status: number): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw failure('protocol', status);
  return value as Record<string, unknown>;
}
function array(value: unknown, status: number): unknown[] {
  if (!Array.isArray(value)) throw failure('protocol', status);
  return value;
}
function text(value: unknown, status: number): string {
  if (typeof value !== 'string' || !value.trim()) throw failure('protocol', status);
  return value;
}
function optionalText(value: unknown, status: number): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'string') throw failure('protocol', status);
  return value;
}
function integer(value: unknown, minimum: number, status: number): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum)
    throw failure('protocol', status);
  return value;
}
function unwrap(value: unknown, status: number): unknown {
  const envelope = object(value, status);
  if (
    typeof envelope.code !== 'string' ||
    typeof envelope.message !== 'string' ||
    !('data' in envelope)
  )
    throw failure('protocol', status);
  if (envelope.code !== '0') throw failure('business', status);
  return envelope.data;
}

function page(value: unknown, requestedPage: number, status: number) {
  const payload = object(value, status);
  const records = array(payload.records, status);
  const total = integer(payload.total, 0, status);
  const size = integer(payload.size, 1, status);
  const current = integer(payload.current, 1, status);
  const pages = integer(payload.pages, 0, status);
  const expectedPages = Math.ceil(total / size);
  if (
    current !== requestedPage ||
    (total === 0 ? current !== 1 || pages > 1 : pages !== expectedPages || current > pages)
  )
    throw failure('protocol', status);
  const expectedRecords = total === 0 ? 0 : Math.min(size, total - (current - 1) * size);
  if (records.length !== expectedRecords) throw failure('protocol', status);
  return { records, total, size, current, pages };
}

async function allPages<T>(
  path: string,
  parameters: Record<string, string | number>,
  token: string,
  signal: AbortSignal,
  wrapped: boolean,
  normalize: (value: unknown, status: number) => T,
  identity: (value: T) => string,
  origin = '',
): Promise<T[]> {
  const result = new Map<string, T>();
  let initial: { total: number; size: number; pages: number } | undefined;
  let count = 0;
  for (let current = 1; ; current++) {
    const response = await getJson(path, { ...parameters, page: current }, token, signal, origin);
    const value = wrapped ? unwrap(response.value, response.status) : response.value;
    const batch = page(value, current, response.status);
    if (
      initial &&
      (initial.total !== batch.total ||
        initial.size !== batch.size ||
        initial.pages !== batch.pages)
    )
      throw failure('protocol', response.status);
    initial ??= batch;
    count += batch.records.length;
    for (const raw of batch.records)
      addUnique(result, normalize(raw, response.status), identity, response.status);
    if (current >= batch.pages) {
      if (count !== batch.total) throw failure('protocol', response.status);
      return [...result.values()];
    }
  }
}

function addUnique<T>(
  result: Map<string, T>,
  item: T,
  identity: (value: T) => string,
  status: number,
) {
  const id = identity(item);
  if (result.has(id) && JSON.stringify(result.get(id)) !== JSON.stringify(item))
    throw failure('protocol', status);
  if (!result.has(id)) result.set(id, item);
}
function normalizeTeam(value: unknown, status: number): PlatformTeam {
  const row = object(value, status);
  return { teamId: text(row.teamId, status), teamName: text(row.teamName, status) };
}
function group(value: string | null): AgentTypeGroup | null {
  if (value === 'base' || value === 'workflow') return 'base/workflow';
  if (value === 'abcclaw' || value === 'abcclaw2') return 'abcclaw';
  return null;
}
function normalizeAgent(value: unknown, status: number): PlatformAgent {
  const row = object(value, status);
  const agentType = optionalText(row.agentType, status);
  const arrangeType = optionalText(row.arrangeType, status);
  const typeGroup = arrangeType ? group(arrangeType) : group(agentType);
  const tools = Array.isArray(row.tools) ? row.tools : [];
  const skills = Array.isArray(row.skills) ? row.skills : [];
  return {
    agentId: text(row.id, status),
    agentName: text(row.name, status),
    typeGroup,
    platformAgentType: agentType,
    platformArrangeType: arrangeType,
    tools,
    skills,
  };
}
function normalizeBranches(
  value: unknown,
  status: number,
  seen = new Set<string>(),
): PlatformBranch[] {
  return array(value, status).map((raw) => {
    const row = object(raw, status);
    const branchId = text(row.branchId, status);
    if (seen.has(branchId)) throw failure('protocol', status);
    seen.add(branchId);
    return {
      branchId,
      branchName: optionalText(row.branchName, status),
      children: normalizeBranches(row.children === undefined ? [] : row.children, status, seen),
    };
  });
}
function normalizeVersions(
  value: unknown,
  status: number,
  branchId?: string,
  versionField: 'agentVersion' | 'updatedAt' = 'agentVersion',
): PlatformVersion[] {
  const result = new Map<string, PlatformVersion>();
  for (const raw of array(value, status)) {
    const row = object(raw, status);
    if (branchId !== undefined && text(row.branchId, status) !== branchId)
      throw failure('protocol', status);
    // 行内基础编排/工作流以 updatedAt 标识版本，状态允许为空。
    if (row.status === null && versionField !== 'updatedAt') throw failure('protocol', status);
    const versionStatus = optionalText(row.status, status);
    const item = {
      agentVersion: text(row[versionField], status),
      ...(versionStatus === null ? {} : { status: versionStatus }),
    };
    addUnique(result, item, (version) => version.agentVersion, status);
  }
  return [...result.values()];
}

function normalizeCapabilities(value: unknown, status: number): PlatformCapability {
  const row = object(value, status);
  const tools = array(row.tools, status);
  const skills = array(row.skills, status);
  const normalizedTools = tools.map((raw) => {
    const tool = object(raw, status);
    return {
      name: text(tool.name, status),
      description: optionalText(tool.description, status),
    };
  });
  const normalizedSkills = skills.map((raw) => {
    const skill = object(raw, status);
    return {
      external_skill_id: text(skill.external_skill_id, status),
      name: text(skill.name, status),
      description: optionalText(skill.description, status),
      tools: array(skill.tools, status).map((name) => text(name, status)),
    };
  });
  return { tools: normalizedTools, skills: normalizedSkills };
}

function normalizeInitializationVariables(
  value: unknown,
  status: number,
): PlatformInitializationVariable[] {
  const data = object(value, status);
  const config = object(data.agent_config, status);
  return array(config.nodes ?? [], status).flatMap((raw) => {
    const node = object(raw, status);
    if (node.type !== 'Start') return [];
    const start = object(node.config ?? {}, status);
    return array(start.variables ?? [], status).flatMap((rawVariable) => {
      const variable = object(rawVariable, status);
      const name = text(variable.name, status);
      if (name === 'BOT_USER_INPUT' || name === 'user_input') return [];
      return [
        {
          name,
          type: typeof variable.type === 'string' ? variable.type : 'str',
          required: variable.required === true,
        },
      ];
    });
  });
}

// 行内目录：origin 由部署环境配置（VITE_AGENT_PLATFORM_ORIGIN / VITE_ABCCLAW_PLATFORM_ORIGIN，
// 空值走同源 /web 代理）。行外目录：固定本地虚拟地址（VITE_LOCAL_PLATFORM_ORIGIN 可覆盖）。
const LOCAL_PLATFORM_ORIGIN: string =
  ((import.meta.env.VITE_LOCAL_PLATFORM_ORIGIN as unknown) as string | undefined) ||
  'http://127.0.0.1:8119';

function createDirectory(
  fixedOrigin: string,
  baseVersionField: 'agentVersion' | 'updatedAt' = 'agentVersion',
): AgentDirectory {
  const getTeams: AgentDirectory['getTeams'] = async ({ token }) =>
    query(token, (signal) =>
      allPages(
        '/web/ops/team/getTeamRole',
        { limit: 300 },
        token,
        signal,
        true,
        normalizeTeam,
        (item) => item.teamId,
        fixedOrigin,
      ),
    );
  const getAgents: AgentDirectory['getAgents'] = async ({ token, teamId }) => {
    const parameters: Record<string, string | number> = { name: '', limit: 1000 };
    if (teamId !== '') {
      validateId(teamId);
      parameters.teamId = teamId;
    }
    return query(token, (signal) =>
      allPages(
        '/web/agent/agents',
        parameters,
        token,
        signal,
        false,
        normalizeAgent,
        (item) => item.agentId,
        fixedOrigin,
      ),
    );
  };
  const getAgentVersions: AgentDirectory['getAgentVersions'] = async ({ token, agentId }) => {
    validateId(agentId);
    return query(token, async (signal) => {
      const response = await getJson(
        '/web/agent/getAgentVersionList',
        { agentId },
        token,
        signal,
        fixedOrigin,
      );
      return normalizeVersions(
        unwrap(response.value, response.status),
        response.status,
        undefined,
        baseVersionField,
      );
    });
  };
  const getBranches: AgentDirectory['getBranches'] = async ({ token, agentId }) => {
    validateId(agentId);
    return query(token, async (signal) => {
      const response = await getJson(
        '/web/abcclaw/v2/branchTree',
        { agentId },
        token,
        signal,
        fixedOrigin,
      );
      return normalizeBranches(unwrap(response.value, response.status), response.status);
    });
  };
  const getBranchVersions: AgentDirectory['getBranchVersions'] = async ({
    token,
    agentId,
    branchId,
  }) => {
    validateId(agentId);
    validateId(branchId);
    return query(token, async (signal) => {
      const response = await getJson(
        '/web/abcclaw/v2/listVersions',
        { agentId, branchId },
        token,
        signal,
        fixedOrigin,
      );
      return normalizeVersions(unwrap(response.value, response.status), response.status, branchId);
    });
  };
  const getCapabilities: AgentDirectory['getCapabilities'] = async ({
    token,
    agentId,
    agentVersion,
    branchId,
  }) => {
    validateId(agentId);
    validateId(agentVersion);
    return query(token, async (signal) => {
      const response = await getJson(
        '/web/agent/capabilities',
        { agentId, agentVersion, ...(branchId ? { branchId } : {}) },
        token,
        signal,
        fixedOrigin,
      );
      return normalizeCapabilities(unwrap(response.value, response.status), response.status);
    });
  };
  const getInitializationVariables: AgentDirectory['getInitializationVariables'] = async ({
    token,
    agentId,
    agentVersion,
  }) => {
    return query(token, async (signal) => {
      const response = await getJson(
        '/web/agent/getAgentConfigJsonObjectByAgentIdAndVersion',
        { agentId, agentVersion },
        token,
        signal,
        fixedOrigin,
      );
      return normalizeInitializationVariables(
        unwrap(response.value, response.status),
        response.status,
      );
    });
  };
  return {
    getTeams,
    getAgents,
    getBranches,
    getAgentVersions,
    getBranchVersions,
    getCapabilities,
    getInitializationVariables,
  };
}

export const agentDirectory: AgentDirectory = createDirectory('', 'updatedAt');
const mockDirectory = createDirectory(LOCAL_PLATFORM_ORIGIN);
interface LocalCatalog {
  targets: LocalTarget[];
  unavailable: { agent_id: string; name: string; mode: string }[];
}
async function localCatalog(token: string): Promise<LocalCatalog> {
  if (token !== 'local') throw failure('invalid_input');
  return request<LocalCatalog>('/local-targets');
}
const isLocalId = (id: string) => ['loan-agent', 'loan-base', 'loan-workflow', 'loan-cloudshrimp'].includes(id);
export const localAgentDirectory: AgentDirectory = {
  ...mockDirectory,
  async getCapabilities(input) {
    if (!isLocalId(input.agentId)) return mockDirectory.getCapabilities!(input);
    const catalog = await localCatalog(input.token);
    const target = catalog.targets.find(t => t.descriptor.ref.external_target_id === input.agentId &&
      t.descriptor.ref.external_version_id === input.agentVersion);
    if (!target) throw failure('business');
    return { tools: target.descriptor.tools ?? [], skills: target.descriptor.skills.map(s => ({
      external_skill_id: s.external_skill_id, name: s.name, description: s.description,
      tools: (s.tools ?? []).map(t => t.name),
    })) };
  },
  async getAgents(input) {
    const [mock, catalog] = await Promise.all([mockDirectory.getAgents(input), localCatalog(input.token)]);
    const local = new Map<string, PlatformAgent>();
    for (const target of catalog.targets) {
      const descriptor = target.descriptor;
      local.set(descriptor.ref.external_target_id, {
        agentId: descriptor.ref.external_target_id,
        agentName: descriptor.display_name,
        typeGroup: target.mode === 'cloudshrimp' ? 'abcclaw' : 'base/workflow',
        platformAgentType: null,
        platformArrangeType: target.mode,
        local: true,
        tools: descriptor.tools?.map(t => ({ function: t })),
        skills: descriptor.skills.map(s => ({ id: s.external_skill_id, name: s.name,
          description: s.description, tools: s.tools?.map(t => t.name) })),
      });
    }
    for (const target of catalog.unavailable) local.set(target.agent_id, {
      agentId: target.agent_id, agentName: target.name,
      typeGroup: target.mode === 'cloudshrimp' ? 'abcclaw' : 'base/workflow',
      platformAgentType: null, platformArrangeType: target.mode, local: true, unavailable: true,
    });
    return [...mock, ...local.values()];
  },
  async getAgentVersions(input) {
    if (!isLocalId(input.agentId)) return mockDirectory.getAgentVersions(input);
    const catalog = await localCatalog(input.token);
    return catalog.targets.filter(t => t.descriptor.ref.external_target_id === input.agentId)
      .map(t => ({ agentVersion: t.descriptor.ref.external_version_id, localTarget: t }));
  },
  async getBranches(input) {
    return isLocalId(input.agentId) ? [] : mockDirectory.getBranches(input);
  },
  async getBranchVersions(input) {
    return isLocalId(input.agentId) ? [] : mockDirectory.getBranchVersions(input);
  },
};
export const getTeams = agentDirectory.getTeams;
