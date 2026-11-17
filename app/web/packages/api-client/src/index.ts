const API = "/api/v1";

/** 班组选项（统一来源，避免各页硬编码漂移）。 */
export const SQUADS = ["1班", "2班", "3班"] as const;
/** 加班时段选项。 */
export const SHIFT_SLOTS = ["早班", "中班", "晚班"] as const;

/** 工时口径：整班 8 小时、半班 4 小时。后端只认数字 workHours，中文仅作展示 label。 */
export const FULL_SHIFT_HOURS = 8;
export const HALF_SHIFT_HOURS = 4;
export function workHoursLabel(h: number | null | undefined, opts?: { day?: boolean }): string {
  const half = opts?.day ? "半天" : "半班";
  const full = opts?.day ? "整天" : "整班";
  return h != null && h < FULL_SHIFT_HOURS ? half : full;
}
export function isHalfShift(h: number | null | undefined): boolean {
  return h != null && h < FULL_SHIFT_HOURS;
}
/**
 * 工时展示（含加班）：>8 时拆分整班 8h + 加班，否则沿用 workHoursLabel（4→半班/8→整班）。
 * 如 12 → 「12小时（整班8h+加班4h）」。
 */
export function overtimeHoursLabel(h: number): string {
  if (h > FULL_SHIFT_HOURS) return `${h}小时（整班8h+加班${h - FULL_SHIFT_HOURS}h）`;
  return workHoursLabel(h);
}

export type ApiErrorBody = {
  detail?: string;
  error?: string;
  duplicate?: boolean;
  formalAllowed?: boolean;
  allowedEmployTypes?: string[];
  // /scan 409「已上岗」附带的上岗上下文
  existingRegion?: string | null;
  existingFurnace?: string | null;
  existingFurnaceType?: string | null;
  existingChief?: string | null;
  [key: string]: unknown;
};

async function parseErrorBody(res: Response): Promise<ApiErrorBody> {
  const text = await res.text();
  try {
    return JSON.parse(text) as ApiErrorBody;
  } catch {
    return { error: text.slice(0, 200) || res.statusText };
  }
}

// 全局请求活动钩子：每个 in-flight 请求计数，0↔1 跳变时通知已注册的处理器。
// H5 注册它来显示/隐藏统一 loading 指示；admin 不注册 → 无影响。
let _activeRequests = 0;
let _activityHandler: ((active: boolean) => void) | null = null;
export function setRequestActivityHandler(fn: ((active: boolean) => void) | null) {
  _activityHandler = fn;
}
function _reqBegin() {
  _activeRequests += 1;
  if (_activeRequests === 1) {
    try {
      _activityHandler?.(true);
    } catch {
      /* ignore */
    }
  }
}
function _reqEnd() {
  _activeRequests = Math.max(0, _activeRequests - 1);
  if (_activeRequests === 0) {
    try {
      _activityHandler?.(false);
    } catch {
      /* ignore */
    }
  }
}

export async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  _reqBegin();
  try {
    const res = await fetch(`${API}${path}`, { credentials: "include", ...init });
    if (!res.ok) {
      const data = await parseErrorBody(res);
      throw Object.assign(new Error(data.error || data.detail || "request failed"), {
        status: res.status,
        retryAfter: res.headers.get("Retry-After") ? Number(res.headers.get("Retry-After")) : undefined,
        data,
      });
    }
    return res.json() as Promise<T>;
  } finally {
    _reqEnd();
  }
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  _reqBegin();
  try {
    const res = await fetch(`${API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok)
      throw Object.assign(new Error(data.error || data.detail || "请求失败"), {
        status: res.status,
        retryAfter: res.headers.get("Retry-After") ? Number(res.headers.get("Retry-After")) : undefined,
        data,
      });
    return data as T;
  } finally {
    _reqEnd();
  }
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  _reqBegin();
  try {
    const res = await fetch(`${API}${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok)
      throw Object.assign(new Error(data.error || data.detail || "请求失败"), {
        status: res.status,
        retryAfter: res.headers.get("Retry-After") ? Number(res.headers.get("Retry-After")) : undefined,
        data,
      });
    return data as T;
  } finally {
    _reqEnd();
  }
}

export type FurnaceItem = {
  id: number;
  /** 数据库唯一编码（如 FUR01），工作点二维码即编码此值 */
  code?: string | null;
  region: string;
  regionCode?: string | null;
  furnaceNo: string;
  furnaceNoOrder?: number;
  furnaceType: string;
  chiefUserId?: string | null;
  chiefName?: string | null;
  chiefs?: { empId: string; name?: string | null }[];
  hasTemplate?: boolean;
  customized?: boolean;
  quotaRules: { squad: string | null; post: string; capacity: number; squadless: boolean }[];
};

export type EmployeeItem = {
  empId: string;
  name: string;
  role: string;
  syncStatus?: string | null;
  employmentStatus: "active" | "departed";
};

export function getEmployees() {
  return apiGet<{ items: EmployeeItem[] }>("/employees");
}

export function searchEmployees(q: string) {
  return apiGet<{ items: EmployeeItem[] }>(`/employees/search?q=${encodeURIComponent(q)}`);
}

export type ChiefWorker = {
  empId: string;
  name?: string | null;
  employmentStatus: "active" | "departed";
};

export function getChiefWorkers(chief?: string) {
  const qs = chief ? `?chief=${encodeURIComponent(chief)}` : "";
  return apiGet<{ chief: string; items: ChiefWorker[] }>(`/chief-workers${qs}`);
}

export function addChiefWorker(empId: string, chiefUserId?: string) {
  return apiPost<{ chief: string; items: ChiefWorker[] }>("/chief-workers", { empId, chiefUserId });
}

export function deleteChiefWorker(chiefUserId: string, empId: string) {
  return apiGet<{ chief: string; items: ChiefWorker[] }>(
    `/chief-workers/${encodeURIComponent(chiefUserId)}/${encodeURIComponent(empId)}`,
    { method: "DELETE" }
  );
}

export function getMyChiefs() {
  return apiGet<{ items: { userid: string; name?: string | null }[] }>("/my-chiefs");
}

// ---- 员工管理（HR）----
export type AdminEmployee = {
  id?: number;
  empId: string;
  name: string;
  ddUserId?: string | null;
  role: string;
  explicitRole?: string | null;
  employmentStatus: "active" | "departed";
  regions?: string[];
  chiefRegions?: string[];
  chiefCandidateEligible?: boolean;
  pushEnabled?: boolean; // 钉钉通知开关（admin 可改，HR 只读）
  hasPassword?: boolean;
  updatedAt?: string | null;
  updatedBy?: string | null;
};
export type EmployeeDetail = AdminEmployee & {
  favorites: { post: string[]; furnace: string[]; region: string[] };
  chiefWorkers?: ChiefWorker[];
  belongChiefs?: ChiefWorker[]; // 被哪些值班长关联（常用员工/代签名单）的反向列表
};
export type EmployeeFavorites = EmployeeDetail["favorites"];

export type EmployeeFilters = {
  q?: string;
  regions?: string[];
  roles?: string[];
  pushEnabled?: boolean[];
  employmentStatus?: AdminEmployee["employmentStatus"];
};

let pageRegionScope: string | undefined;

/** 正常管理页面的展示地区；仅用于前端过滤，不是后端权限控制。 */
export function setPageRegionScope(region?: string) {
  pageRegionScope = region || undefined;
}

export function getPageRegionScope() {
  return pageRegionScope;
}

export function getAdminEmployees(filters: EmployeeFilters = {}) {
  const p = new URLSearchParams();
  if (filters.q?.trim()) p.set("q", filters.q.trim());
  const regions = pageRegionScope ? [pageRegionScope] : filters.regions;
  regions?.forEach((v) => p.append("regions", v));
  filters.roles?.forEach((v) => p.append("roles", v));
  filters.pushEnabled?.forEach((v) => p.append("push_enabled", String(v)));
  if (filters.employmentStatus) p.set("employment_status", filters.employmentStatus);
  const qs = p.toString();
  return apiGet<{ items: AdminEmployee[]; candidateContractVersion?: number }>(
    `/admin/employees${qs ? `?${qs}` : ""}`
  );
}
export function getChiefDepartedEmployeeVisibility() {
  return apiGet<{ showDepartedToChief: boolean }>(
    "/admin/employees/chief-departed-visibility"
  );
}
export function setChiefDepartedEmployeeVisibility(showDepartedToChief: boolean) {
  return apiPut<{ showDepartedToChief: boolean }>(
    "/admin/employees/chief-departed-visibility",
    { showDepartedToChief }
  );
}
export function getAdminEmployee(empId: string) {
  return apiGet<EmployeeDetail>(`/admin/employees/${encodeURIComponent(empId)}`);
}
export function createEmployee(body: { empId: string; name: string; ddUserId?: string; role?: string; favorites?: EmployeeFavorites }) {
  return apiPost<EmployeeDetail>("/admin/employees", body);
}
export function updateEmployee(
  empId: string,
  body: { name?: string; ddUserId?: string; role?: string; pushEnabled?: boolean; favorites?: EmployeeFavorites }
) {
  return apiPut<EmployeeDetail>(`/admin/employees/${encodeURIComponent(empId)}`, body);
}

/** 本人钉钉通知开关（h5 个人设置页）。 */
export function getMyNotifySetting() {
  return apiGet<{ pushEnabled: boolean }>("/me/notify-setting");
}
export function setMyNotifySetting(pushEnabled: boolean) {
  return apiPost<{ pushEnabled: boolean }>("/me/notify-setting", { pushEnabled });
}
export function deleteEmployee(empId: string) {
  return apiGet<{ ok: boolean }>(`/admin/employees/${encodeURIComponent(empId)}`, {
    method: "DELETE",
  });
}
export function setEmployeeFavorites(empId: string, kind: string, values: string[]) {
  return apiPut<{ ok: boolean; favorites: { post: string[]; furnace: string[]; region: string[] } }>(
    `/admin/employees/${encodeURIComponent(empId)}/favorites`,
    { kind, values }
  );
}

export type DemoContext = {
  t2: string;
  t1: string;
  today: string;
  month: string;
};

export function getDemoContext() {
  return apiGet<DemoContext>("/demo-context");
}

export function getFurnaceTypes() {
  // 设备类型字典（只读，登录即可）：工作点编制页筛选/模板卡片数据源
  return apiGet<{ items: { code: string; name: string }[] }>("/furnace-types");
}

export function getFurnaces() {
  // 后端 /furnaces 已按地区权威收窄；前端不再二次按 pageRegionScope 过滤，避免 pageRegionScope
  // 来源（/me.region）被值班长分支覆写后与后端不一致、静默过滤成空。
  return apiGet<{ items: FurnaceItem[] }>("/furnaces");
}

export function updateFurnaceConfig(
  furnaceId: number,
  body: { chiefIds?: string[]; furnaceType?: string; region?: string; furnaceNo?: string }
) {
  // 工作点配置：值班长绑定（整组替换）、设备类型、地区/工作点编号修改（HR）
  return apiPut<{ ok: boolean }>(`/furnaces/${furnaceId}/config`, body);
}

export function getQuota(params: Record<string, string>, natualDate?: string) {
  const p = { ...params };
  if (natualDate) p.date = natualDate;
  const q = new URLSearchParams(p).toString();
  return apiGet<{
    capacity: number;
    formalUsed: number;
    formalAllowed: boolean;
    squadless: boolean;
    allowedEmployTypes?: string[];
  }>(`/quota?${q}`);
}

export type Me = {
  adminTestBanner?: boolean;
  userid: string;
  role: string;
  name?: string;
  title?: string;
  empId?: string;
  furnace?: string;
  region?: string;
  furnaces?: { region: string; furnaceNo: string }[];
  super?: boolean;
  impersonating?: boolean;
  regionConfirmed?: boolean;
  requiresRegionSelection?: boolean;
  regionConflict?: boolean;
  regionMessage?: string;
};

export function getMe() {
  return apiGet<Me>("/me");
}

export function getRegions() {
  return apiGet<{ items: { code: string; name: string }[] }>("/regions");
}

export function confirmMyRegion(region: string) {
  return apiPost<{ ok: boolean; region: string; regionConfirmed: boolean }>("/me/region", {
    region,
  });
}

export function login(userid: string, role?: string) {
  return apiPost<{ userid: string; role: string }>("/auth/login", { userid, role });
}

export function logout() {
  return apiPost<{ ok: boolean }>("/auth/logout", {});
}

// 超级管理员（浏览器入口）：TOTP 动态码登录 + 身份切换
export function adminLogin(code: string) {
  return apiPost<{ userid: string; role: string; super: boolean }>("/auth/admin-login", {
    code,
  });
}

export type SwitchEmployee = {
  empId: string;
  name: string;
  role: string;
  ddUserId?: string | null;
};

export function switchEmployees(q?: string) {
  const qs = q ? `?q=${encodeURIComponent(q)}` : "";
  return apiGet<{ items: SwitchEmployee[] }>(`/admin/switch/employees${qs}`);
}

export function impersonate(empId: string) {
  return apiPost<{ userid: string; name: string; role: string }>(
    "/admin/switch/impersonate",
    { empId }
  );
}

export function exitImpersonate() {
  return apiPost<{ userid: string; role: string; super: boolean }>("/admin/switch/exit", {});
}

export type DictItem = {
  code: string;
  name: string;
  sort: number;
  updatedAt?: string | null;
  updatedBy?: string | null;
  // 工作点字典专用
  region?: string;
  regionCode?: string | null;
  furnaceType?: string;
  status?: string;
};
export function getDict(kind: string) {
  return apiGet<{ items: DictItem[] }>(`/admin/dict/${kind}`);
}
export function createDict(
  kind: string,
  body: { name: string; code?: string; sort?: number; regionCode?: string; furnaceType?: string }
) {
  return apiPost<{ ok: boolean; code: string }>(`/admin/dict/${kind}`, body);
}
export function updateDict(
  kind: string,
  code: string,
  body: { name?: string; sort?: number; furnaceType?: string; status?: string }
) {
  return apiPut<{ ok: boolean }>(`/admin/dict/${kind}/${encodeURIComponent(code)}`, body);
}
export function deleteDict(kind: string, code: string) {
  return apiGet<{ ok: boolean }>(`/admin/dict/${kind}/${encodeURIComponent(code)}`, {
    method: "DELETE",
  });
}

// ---- 考勤对照（扫码 vs 钉钉打卡 双轨时间轴）----
export type TimelineScan = {
  time: string;
  region?: string | null;
  furnace?: string | null;
  squad?: string | null;
  post: string;
  employType: string;
  source?: string | null;
  origin?: string; // 扫码 | 补录通过 | 代签
  workHours?: number; // 4=半班，8=整班
};
export type TimelinePendingMakeup = {
  region?: string | null;
  furnace?: string | null;
  squad?: string | null;
  post: string;
  employType: string;
  reason?: string | null;
  workHours?: number;
};
export type TimelineOvertime = {
  id: number;
  time?: string | null;
  region?: string | null;
  furnace?: string | null;
  squad?: string | null;
  shiftSlot?: string | null;
  post: string;
  employType: string;
  workHours: number;
  reconDingtalk?: boolean;
  checkWindow?: {
    covered: boolean;
    windowStart?: string | null;
    windowEnd?: string | null;
    shiftStart?: string | null;
    shiftEnd?: string | null;
    punches: TimelinePunch[];
  };
};
export type TimelinePunch = {
  id?: number;
  time: string;
  inoutType: string;
  source?: string | null;
  selectedForCalculation?: boolean;
  calculationRole?: "start" | "end" | null;
};
export type ReconTimelineItem = {
  empId: string;
  name: string;
  status: string;
  timeDiffMinutes?: number | null;
  scans: TimelineScan[];
  overtimes?: TimelineOvertime[];
  pendingMakeups: TimelinePendingMakeup[];
  punches: TimelinePunch[];
};
export type SquadScheduleItem = {
  squad: string;
  from: string;
  to: string;
  crossesMidnight: boolean;
};
export type ReconTimeline = {
  date: string;
  squadSchedules: { region: string; squads: SquadScheduleItem[] }[];
  items: ReconTimelineItem[];
};

export function getReconTimeline(params: {
  date: string;
  region?: string;
  furnace?: string;
  squad?: string;
  status?: string;
  q?: string;
}) {
  const entries = Object.entries(params).filter(([, v]) => v) as [string, string][];
  const qs = new URLSearchParams(entries).toString();
  return apiGet<ReconTimeline>(`/recon/timeline?${qs}`);
}

export type ProxyQuota = {
  empId: string;
  month: string;
  used: number;
  default: number;
  extra: number;
  limit: number;
  remaining: number;
};

/**
 * 跨岗位加班（模式2）：给当天已有记录的工人在指定班组/时段的另一岗位再上岗。
 * 成功 201 → {ok,id,workHours}；失败 4xx，错误信息在 detail。
 */
export function addOvertime(body: {
  empId: string;
  date: string;
  furnace: string;
  region?: string;
  squad: string | null;
  shiftSlot: string;
  post: string;
  employType: string;
  workHours: number;
}) {
  return apiPost<{ ok: boolean; id: number; workHours: number }>("/attendance/overtime", body);
}

// 加班搜索任意工人后，查其当天是否已有记录及已占班组/时段（加班要求已有记录）
export function getWorkerDay(empId: string, date: string) {
  const q = new URLSearchParams({ empId, date }).toString();
  return apiGet<{
    empId: string;
    name?: string;
    hasRecord: boolean;
    squads: string[];
    baseSquad?: string | null;
    slots?: string[];
    usedHours?: number;
    remainingHours?: number;
  }>(
    `/attendance/worker-day?${q}`
  );
}

export function getProxyQuota(empId: string, date: string) {
  const q = new URLSearchParams({ empId, date }).toString();
  return apiGet<ProxyQuota>(`/makeup/proxy-quota?${q}`);
}

export function getProxyConfig() {
  return apiGet<{ defaultLimit: number }>("/admin/proxy-signin/config");
}

export function setProxyConfig(value: number) {
  return apiPut<{ defaultLimit: number }>("/admin/proxy-signin/config", { value });
}

export type ProxyUsageRow = {
  empId: string;
  name: string | null;
  used: number;
  default: number;
  extra: number;
  limit: number;
  remaining: number;
  atLimit: boolean;
};

export function getProxyUsage(month: string) {
  const q = new URLSearchParams({ month }).toString();
  return apiGet<{ month: string; default: number; items: ProxyUsageRow[] }>(
    `/admin/proxy-signin/usage?${q}`
  );
}

export function grantProxyExtra(empId: string, month: string, extra: number, reason?: string) {
  return apiPost<ProxyQuota>("/admin/proxy-signin/grant", { empId, month, extra, reason });
}

export type AuditLogItem = {
  id: number;
  ts: string | null;
  actor: string;
  actorName: string | null;
  actorRole: string;
  actorRoleLabel: string;
  action: string;
  actionLabel: string;
  empId: string | null;
  empName: string | null;
  furnace: string | null;
  date: string | null;
  detail: string;
  requestStatus?: number;
  requestCategory?: string;
  requestDetail?: {
    category?: string;
    status?: number;
    durationMs?: number;
    actorRole?: string;
    error?: string;
    request?: {
      method?: string;
      path?: string;
      query?: Record<string, unknown>;
      headers?: Record<string, string>;
      body?: unknown;
      client?: string | null;
    };
    [key: string]: unknown;
  };
};

export function getAuditLogs(
  opts: {
    action?: string;
    actor?: string;
    target?: string;
    detail?: string;
    start?: string;
    end?: string;
    limit?: number;
  } = {},
) {
  const p = new URLSearchParams();
  if (opts.action) p.set("action", opts.action);
  if (opts.actor) p.set("actor", opts.actor);
  if (opts.target) p.set("target", opts.target);
  if (opts.detail) p.set("detail", opts.detail);
  if (opts.start) p.set("start", opts.start);
  if (opts.end) p.set("end", opts.end);
  p.set("limit", String(opts.limit ?? 200));
  return apiGet<{
    items: AuditLogItem[];
    actions: { value: string; label: string }[];
  }>(`/audit-logs?${p.toString()}`);
}

export type EscalationItem = {
  id: number;
  furnaceId: number;
  date: string;
  level: number;
  targets: string;
  status: string;
};

export function getEscalations(date?: string) {
  return apiGet<{ items: EscalationItem[] }>(
    `/admin/escalations${date ? `?date=${date}` : ""}`,
  );
}

export function runEscalation(date: string) {
  return apiPost<unknown>(`/admin/run-escalation?date=${date}`, {});
}

export type MyNotification = {
  id: number;
  ts: string | null;
  subject: string;
  kind: string;
  detail: string;
  pushNote?: string; // 如「钉钉推送失败：该员工无钉钉号」
};

export function getMyNotifications(limit = 30) {
  return apiGet<{ items: MyNotification[]; count: number }>(
    `/my-notifications?limit=${limit}`,
  );
}

/** 角色字典（与后端 role.code 对齐）：扫码「可见角色」多选用。 */
export const ROLE_OPTIONS: { value: string; label: string }[] = [
  { value: "worker", label: "操作工（worker）" },
  { value: "chief", label: "值班长（chief）" },
  { value: "hr", label: "人事专员（hr）" },
  { value: "stat", label: "统计员（stat）" },
  { value: "admin", label: "系统管理员（admin）" },
  { value: "repairer", label: "维修工（repairer）" },
  { value: "leader", label: "班组长（leader）" },
];

export function getSelectablePosts(region: string, furnace: string, role?: string) {
  const params: Record<string, string> = { region, furnace };
  if (role) params.role = role;
  const q = new URLSearchParams(params).toString();
  return apiGet<{ posts: string[]; visible?: { post: string; roles: string[] }[] }>(
    `/furnaces/selectable-posts?${q}`
  );
}

export type ScanSuggest = {
  squad: string | null;
  squadless: boolean;
  employType: string;
  post: string;
};

/** 扫码上岗自动预选：班组/用工类型/工种（不分班优先；工种取常用→上次且可选时） */
export function getScanSuggest(region: string, furnace: string, empId: string) {
  const q = new URLSearchParams({ region, furnace, empId }).toString();
  return apiGet<ScanSuggest>(`/scan/suggest?${q}`);
}

export function updateFurnaceQuota(
  furnaceId: number,
  rules: { squad: string | null; post: string; capacity: number }[]
) {
  return apiPut<{ ok: boolean }>(`/furnaces/${furnaceId}/quota`, { rules });
}

export function getPostCatalog() {
  return apiGet<{ items: string[] }>("/post-catalog");
}

export type RoleItem = { code: string; name: string; sort: number };

/** 角色字典（基础字典）：供页面统一显示角色名，避免漏映射。 */
export function getRoles() {
  return apiGet<{ items: RoleItem[] }>("/roles");
}

export function addPostCatalog(name: string) {
  return apiPost<{ items: string[] }>("/post-catalog", { name });
}

export function deletePostCatalog(name: string) {
  return apiGet<{ items: string[] }>(`/post-catalog/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export function overwriteFurnaceQuota(
  furnaceId: number,
  rules: { squad: string | null; post: string; capacity: number }[],
  visiblePosts: { post: string; roles: string[] }[] = []
) {
  return apiPut<{ ok: boolean }>(`/furnaces/${furnaceId}/quota-overwrite`, {
    rules,
    visiblePosts,
  });
}

export function updateSelectablePosts(region: string, posts: string[], furnaceNo?: string | null) {
  return apiPut<{ ok: boolean }>("/furnaces/selectable-posts", {
    region,
    furnaceNo: furnaceNo ?? null,
    posts,
  });
}

export type QuotaTemplateItem = {
  id?: number;
  region?: string | null;
  inherited?: boolean;
  post: string;
  capacity: number;
  capacity2?: number;
  capacity3?: number;
  squadless: boolean;
  visibleRoles?: string[];
};

export function getQuotaTemplates(furnaceType: string, region?: string | null) {
  const params: Record<string, string> = { furnaceType };
  if (region) params.region = region;
  const q = new URLSearchParams(params).toString();
  return apiGet<{ furnaceType: string; region: string | null; items: QuotaTemplateItem[] }>(
    `/admin/quota-templates?${q}`
  );
}

export function putQuotaTemplates(
  furnaceType: string,
  items: {
    post: string;
    capacity: number;
    capacity2?: number;
    capacity3?: number;
    squadless: boolean;
    visibleRoles?: string[];
  }[],
  region?: string | null
) {
  return apiPut<{ furnaceType: string; region: string | null; items: QuotaTemplateItem[] }>(
    "/admin/quota-templates",
    { furnaceType, region: region ?? null, items }
  );
}

export function applyQuotaTemplate(
  furnaceType: string,
  furnaceNos?: string[],
  region?: string | null,
) {
  return apiPost<{ furnaceType: string; applied: string[] }>(
    "/admin/quota-templates/apply",
    { furnaceType, furnaceNos: furnaceNos ?? null, region: region ?? null }
  );
}

/**
 * 解析工作点二维码内容。
 * 新格式（推荐）：`FDC:<code>`，code 为工作点数据库唯一编码（如 FUR01），无分隔符。
 * 旧格式（兼容）：`FDC:<region>|<furnaceNo>|<sig>` 或 `region|furnaceNo`。
 */
export function parseScanPayload(
  raw: string
): { code?: string; region?: string; furnaceNo?: string } | null {
  let payload = raw.trim();
  if (payload.startsWith("FDC:")) {
    payload = payload.slice(4);
  }
  if (!payload) return null;
  const parts = payload.split("|");
  if (parts.length === 1) {
    // 单段 = 工作点唯一编码
    return { code: parts[0] };
  }
  // 旧格式：region|furnaceNo|sig
  return { region: parts[0], furnaceNo: parts[1] };
}

/** 生成工作点二维码内容：编码工作点数据库唯一 code（如 FUR01）。 */
export function buildScanPayload(code: string): string {
  return `FDC:${code}`;
}

export type ExportButtonKey =
  | "combined"
  | "payroll"
  | "payroll2"
  | "anomaly"
  | "tripartite"
  | "hours"
  | "overtime"
  | "overtimeSummary"
  | "overtimePerson";
export type ExportVisibleRole = "hr" | "stat";
export type ExportButtonVisibility = Record<ExportButtonKey, ExportVisibleRole[]>;

export function getExportButtonVisibility() {
  return apiGet<{ buttons: ExportButtonVisibility }>("/export/button-visibility");
}

export function setExportButtonVisibility(buttons: ExportButtonVisibility) {
  return apiPut<{ buttons: ExportButtonVisibility }>(
    "/admin/export/button-visibility",
    { buttons }
  );
}

export async function downloadExport(
  month: string,
  furnaces: string[],
  furnaceIds?: number[],
  payroll2GroupBy?: string[],
  payroll2IncludeZeroRows = false,
  payroll2MergeOvertimeOnlyRows = false,
  payroll2MergeSameSquadOvertimeOnlyRows = false,
): Promise<string> {
  const res = await fetch("/api/v1/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ month, furnaces, furnaceIds, payroll2GroupBy, payroll2IncludeZeroRows, payroll2MergeOvertimeOnlyRows, payroll2MergeSameSquadOvertimeOnlyRows }),
  });
  if (!res.ok) {
    const data = await parseErrorBody(res);
    throw Object.assign(new Error(data.error || data.detail || "export failed"), {
      status: res.status,
        retryAfter: res.headers.get("Retry-After") ? Number(res.headers.get("Retry-After")) : undefined,
      data,
    });
  }
  const blob = await res.blob();
  const filename = `三班倒打卡_${month}.xlsx`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return filename;
}

export async function downloadExportSheet(
  month: string,
  sheet: string,
  furnaces: string[],
  furnaceIds?: number[],
  payroll2GroupBy?: string[],
  payroll2IncludeZeroRows = false,
  payroll2MergeOvertimeOnlyRows = false,
  payroll2MergeSameSquadOvertimeOnlyRows = false,
): Promise<string> {
  const res = await fetch("/api/v1/export/sheet", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ month, sheet, furnaces, furnaceIds, payroll2GroupBy, payroll2IncludeZeroRows, payroll2MergeOvertimeOnlyRows, payroll2MergeSameSquadOvertimeOnlyRows }),
  });
  if (!res.ok) {
    const data = await parseErrorBody(res);
    throw Object.assign(new Error(data.error || data.detail || "export failed"), {
      status: res.status,
        retryAfter: res.headers.get("Retry-After") ? Number(res.headers.get("Retry-After")) : undefined,
      data,
    });
  }
  const blob = await res.blob();
  const filename = `${sheet}_${month}.xlsx`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return filename;
}

/** 劳勤工作台月度导出：考勤表式日历——一人(工作点×岗位)一行，列含 地区/工作点编号/姓名/工号/岗位 +
 *  当月每日有效工时 + 全月 + 加班。format 默认 calendar；flat=旧式合并汇总。 */
export async function downloadMonthlyHours(
  month: string,
  format: "calendar" | "flat" = "calendar",
  furnaceIds?: number[]
): Promise<string> {
  const fp = furnaceIds && furnaceIds.length ? `&furnaceIds=${furnaceIds.join(",")}` : "";
  const res = await fetch(
    `${API}/export/monthly-hours?month=${encodeURIComponent(month)}&format=${format}${fp}`,
    { credentials: "include" }
  );
  if (!res.ok) {
    const data = await parseErrorBody(res);
    throw Object.assign(new Error(data.error || data.detail || "导出失败"), {
      status: res.status,
        retryAfter: res.headers.get("Retry-After") ? Number(res.headers.get("Retry-After")) : undefined,
      data,
    });
  }
  const blob = await res.blob();
  const filename = `劳勤工作台_班次有效工时_${month}.xlsx`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return filename;
}

/** 月度加班明细表导出：逐条明细 + 加班汇总(工作点编号×岗位×班次)工作表。 */
export async function downloadOvertimeDetail(month: string, furnaceIds?: number[]): Promise<string> {
  const fp = furnaceIds && furnaceIds.length ? `&furnaceIds=${furnaceIds.join(",")}` : "";
  const res = await fetch(
    `${API}/export/overtime-detail?month=${encodeURIComponent(month)}${fp}`,
    { credentials: "include" }
  );
  if (!res.ok) {
    const data = await parseErrorBody(res);
    throw Object.assign(new Error(data.error || data.detail || "导出失败"), {
      status: res.status,
        retryAfter: res.headers.get("Retry-After") ? Number(res.headers.get("Retry-After")) : undefined,
      data,
    });
  }
  const blob = await res.blob();
  const filename = `加班明细表_${month}.xlsx`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return filename;
}

// ---- 月度导出预览（format=meta：返回计算结果 JSON，不下载文件）----
export type ReportRequestStatus = {
  capacity: number; paused: boolean;
  items: { userId: string; userName: string; task: string; requestedAt: string; elapsedSeconds: number }[];
};
export function getReportRequestStatus() {
  return apiGet<ReportRequestStatus>("/export/request-status");
}

export type AbsenceOverride = {
  days: number | null; maxDays: number; revision: number; editable: boolean; reason: string; updatedBy?: string | null;
};
export function getAbsenceOverrides(month: string, furnaceIds: number[]) {
  return apiGet<{ items: Record<string, AbsenceOverride> }>(
    `/export/absence-overrides?${new URLSearchParams({ month, furnaceIds: furnaceIds.join(",") })}`,
  );
}
export function saveAbsenceOverride(month: string, empId: string, days: number | null, revision: number) {
  return apiPut<AbsenceOverride>("/export/absence-overrides", { month, empId, days, revision });
}

export type PayrollRow = Record<string, string | number>;
export type ExtraSheet = { columns: string[]; rows: (string | number)[][] };
export type PayrollSheetMeta = {
  name: string;
  groupBy?: string[];
  columnCount: number;
  columns: string[];
  sampleRow: PayrollRow;
  rows: PayrollRow[];
  rowCount: number;
};
export type ExportWarning = {
  type: string;
  message: string;
  count?: number;
  employeeIds?: string[];
};
export type ExportMeta = {
  sheets: string[];
  furnaceCount: number;
  warnings: ExportWarning[];
  payrollSheet: PayrollSheetMeta;
  payrollSheet2: PayrollSheetMeta;
  extraSheets: Record<string, ExtraSheet>;
  monthlyHours: MonthlyHoursMeta;
  overtimeDetail: OvertimeDetailMeta;
  billing: { ratios: Record<string, number>; rules: string[] };
};

export type ExportSheetPreviewMeta = {
  sheet: string;
  furnaceCount: number;
  warnings: ExportWarning[];
  payrollSheet?: ExportMeta["payrollSheet"];
  extraSheet?: ExtraSheet;
};

/** 预览导出：用与导出完全一致的口径计算（含对账物化），返回结构化结果供列表展示。 */
export function previewExport(
  month: string,
  furnaces: string[],
  furnaceIds?: number[],
  empId?: string,
  payroll2GroupBy?: string[],
  payroll2IncludeZeroRows = false,
  payroll2MergeOvertimeOnlyRows = false,
  payroll2MergeSameSquadOvertimeOnlyRows = false,
): Promise<ExportMeta> {
  return apiPost<ExportMeta>("/export", {
    month,
    furnaces,
    furnaceIds,
    empId,
    format: "meta",
    payroll2GroupBy,
    payroll2IncludeZeroRows,
    payroll2MergeOvertimeOnlyRows,
    payroll2MergeSameSquadOvertimeOnlyRows,
  });
}

export function previewExportSheet(
  month: string,
  sheet: string,
  furnaces: string[],
  furnaceIds?: number[],
  empId?: string,
  payroll2GroupBy?: string[],
  payroll2IncludeZeroRows = false,
  payroll2MergeOvertimeOnlyRows = false,
  payroll2MergeSameSquadOvertimeOnlyRows = false,
): Promise<ExportSheetPreviewMeta> {
  return apiPost<ExportSheetPreviewMeta>("/export/sheet-preview", {
    month,
    sheet,
    furnaces,
    furnaceIds,
    empId,
    payroll2GroupBy,
    payroll2IncludeZeroRows,
    payroll2MergeOvertimeOnlyRows,
    payroll2MergeSameSquadOvertimeOnlyRows,
  });
}

/** 班次有效工时（calendar 口径）预览 meta：columns = 6 固定列 + 每日 + 汇总列。 */
export type MonthlyHoursMeta = {
  columns: string[];
  rows: (string | number)[][];
  month: string;
  daysInMonth: number;
};

/** 加班明细预览 meta：明细 + 加班汇总 + 按人汇总 三个工作表的数据。 */
export type OvertimeDetailMeta = {
  columns: string[];
  rows: (string | number)[][];
  month: string;
  summaryColumns: string[];
  summaryRows: (string | number)[][];
  byPersonColumns: string[];
  byPersonRows: (string | number)[][];
};

/** 预览「班次有效工时」导出：与下载完全同口径（只读，不落库），返回表格数据。 */
export function previewMonthlyHours(month: string, furnaceIds?: number[]): Promise<MonthlyHoursMeta> {
  const fp = furnaceIds && furnaceIds.length ? `&furnaceIds=${furnaceIds.join(",")}` : "";
  return apiGet<MonthlyHoursMeta>(
    `/export/monthly-hours?month=${encodeURIComponent(month)}&format=meta${fp}`
  );
}

/** 预览「加班明细」导出：与下载完全同口径（只读，不落库），返回三个分页数据。 */
export function previewOvertimeDetail(month: string, furnaceIds?: number[]): Promise<OvertimeDetailMeta> {
  const fp = furnaceIds && furnaceIds.length ? `&furnaceIds=${furnaceIds.join(",")}` : "";
  return apiGet<OvertimeDetailMeta>(
    `/export/overtime-detail?month=${encodeURIComponent(month)}&format=meta${fp}`
  );
}

export type MonthlyExportFurnaceLockStatus = {
  month: string;
  furnaceId: number;
  locked: boolean;
  lockedBy: string | null;
  lockedAt: string | null;
  unlockedBy: string | null;
  unlockedAt: string | null;
};

export type MonthlyExportLockSummary = {
  month: string;
  lockedCount: number;
  totalCount: number;
};

export type MonthlyExportLockStatus = MonthlyExportLockSummary & {
  items: MonthlyExportFurnaceLockStatus[];
};

export function getMonthlyExportLock(month: string, furnaceId?: number): Promise<MonthlyExportLockStatus> {
  const fp = furnaceId == null ? "" : `&furnaceId=${encodeURIComponent(String(furnaceId))}`;
  return apiGet<MonthlyExportLockStatus>(`/export/month-lock?month=${encodeURIComponent(month)}${fp}`);
}

export function getMonthlyExportLocks(year: string): Promise<{ year: string; items: MonthlyExportLockSummary[] }> {
  return apiGet<{ year: string; items: MonthlyExportLockSummary[] }>(
    `/export/month-locks?year=${encodeURIComponent(year)}`
  );
}

export function setMonthlyExportLock(month: string, furnaceIds: number[], locked: boolean): Promise<MonthlyExportLockStatus> {
  return apiPut<MonthlyExportLockStatus>("/export/month-lock", { month, furnaceIds, locked });
}

// ---- 班次结束推送（超管） ----
export type ShiftPushConfig = { enabled: boolean; timePoints: string[]; excludedEmpIds: string[] };
export type ShiftPushRecipient = {
  empId: string; name: string; ddUserId: string; hasDing: boolean; enabled: boolean; furnaces: string[];
};
export type ShiftPushRecord = {
  id: number; ts: string | null; status: string; timePoint?: string; date?: string;
  region?: string; furnace?: string; squad?: string; chiefEmpId?: string; chiefName?: string;
  ddUserId?: string; reason?: string; text?: string;
};

export function getShiftPushConfig() {
  return apiGet<ShiftPushConfig>("/admin/shift-push/config");
}
export function putShiftPushConfig(body: ShiftPushConfig) {
  return apiPut<ShiftPushConfig>("/admin/shift-push/config", body);
}
export function getShiftPushRecipients() {
  return apiGet<{ items: ShiftPushRecipient[] }>("/admin/shift-push/recipients");
}
export function getShiftPushRecords(date?: string) {
  return apiGet<{ items: ShiftPushRecord[] }>(`/admin/shift-push/records${date ? `?date=${date}` : ""}`);
}
export function runShiftPush(body: { timePoint?: string; date?: string }) {
  return apiPost<{ date: string; results: { timePoint: string; sent?: number; skipped?: number; records: number }[] }>(
    "/admin/shift-push/run",
    body
  );
}

// ---- 钉钉考勤同步（HR/超管）：原始打卡同步 + 转劳勤工作台数据 ----
export type DingtalkAttRaw = {
  id: number;
  code: string;
  name: string | null;
  org: string | null;
  layer: string | null;
  attTime: string | null;
  direct: string | null;
  state: string | null;
  src: string | null;
  syncedAt: string | null;
};

export type ExternalAttendanceResultRaw = {
  sourceWithdrawnAt?: string | null;
  id: number;
  tenantCode: string | null;
  deptName: string | null;
  layerName: string | null;
  hrStatus: string | null;
  hrType: string | null;
  userCode: string;
  userName: string | null;
  kqDateShow: string | null;
  kqDateTitle: string | null;
  kqDate: string;
  kqqs: string | null;
  statusName: string | null;
  lxType: string | null;
  actualOn: string | null;
  actualOff: string | null;
  quota: string | null;
  cqgs: string | null;
  kggs: string | null;
  cd2: string | null;
  zt2: string | null;
  ot1: string | null;
  ot2: string | null;
  ot3: string | null;
  leave01: string | null;
  leave02: string | null;
  leave03: string | null;
  leave04: string | null;
  leave05: string | null;
  leave06: string | null;
  leave07: string | null;
  leave09: string | null;
  leave10: string | null;
  leave12: string | null;
  leave13: string | null;
  ext1: string | null;
  ext2: string | null;
  business01: string | null;
  business02: string | null;
  syncedAt: string | null;
};

/** 同步指定范围的钉钉原始考勤打卡到本系统（幂等；整段一次查询，仅适合小范围/兼容调用）。 */
export function syncDingtalkAttendance(body: { dateFrom: string; dateTo: string }) {
  return apiPost<{ dateFrom: string; dateTo: string; fetched: number; upserted: number }>(
    "/admin/dingtalk-attendance/sync",
    body
  );
}

/** 非阻塞启动「按现存员工工号拉取+转劳勤」后台任务；已在跑则返回 alreadyRunning。 */
export function pullDingtalkAttendance(body: { dateFrom: string; dateTo: string }) {
  return apiPost<{ started: boolean; alreadyRunning: boolean }>(
    "/admin/dingtalk-attendance/pull",
    body
  );
}

/** 非阻塞启动后台劳勤转换（按天推进）；已在跑则返回 alreadyRunning。 */
export function transformDingtalkAttendance(body: { dateFrom: string; dateTo: string }) {
  return apiPost<{ started: boolean; alreadyRunning: boolean }>(
    "/admin/dingtalk-attendance/transform",
    body
  );
}

export type AttendanceTransformStatus = {
  status: "idle" | "running" | "done" | "error";
  phase: string | null; // 当前日期
  total: number; // 总天数
  done: number; // 已完成天数
  percent: number;
  message: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  updatedAt: string | null;
};

/** 劳勤转换后台任务进度（前端 2 秒轮询）。 */
export function getDingtalkAttendanceTransformStatus() {
  return apiGet<AttendanceTransformStatus>("/admin/dingtalk-attendance/transform-status");
}

export type AttendanceSyncLog = {
  id: number;
  ts: string | null;
  actor: string;
  trigger: string | null; // auto=定时 / manual=手动
  dateFrom: string | null;
  dateTo: string | null;
  fetched: number | null;
  upserted: number | null;
  attendanceResultFetched: number | null;
  attendanceResultUpserted: number | null;
  transformInserted: number | null;
  matched: number | null;
  ok: boolean | null;
  error: string | null;
};

/** 考勤同步日志（定时/手动同步的执行记录）。 */
export function getDingtalkAttendanceSyncLogs(limit = 50) {
  return apiGet<{ items: AttendanceSyncLog[] }>(
    `/admin/dingtalk-attendance/sync-logs?limit=${limit}`
  );
}

/** 列出已同步的原始考勤打卡（表格展示）。 */
export function getDingtalkAttendanceRaw(opts: {
  dateFrom: string;
  dateTo: string;
  limit?: number;
  employeeId?: string;
  employeeName?: string;
}) {
  const p = new URLSearchParams();
  p.set("dateFrom", opts.dateFrom);
  p.set("dateTo", opts.dateTo);
  p.set("limit", String(opts.limit ?? 500));
  if (opts.employeeId) p.set("employeeId", opts.employeeId);
  if (opts.employeeName) p.set("employeeName", opts.employeeName);
  return apiGet<{ dateFrom: string; dateTo: string; total: number; count: number; items: DingtalkAttRaw[] }>(
    `/admin/dingtalk-attendance/raw?${p.toString()}`
  );
}

/** 非阻塞启动「按本地现存员工工号拉取劳勤考勤结果」后台任务（view_external_att_result）。 */
export function pullExternalAttendanceResult(body: { dateFrom: string; dateTo: string }) {
  return apiPost<{ started: boolean; alreadyRunning: boolean }>(
    "/admin/dingtalk-attendance/result/pull",
    body
  );
}

/** 劳勤考勤结果后台拉取进度。 */
export function getExternalAttendanceResultStatus() {
  return apiGet<AttendanceTransformStatus>("/admin/dingtalk-attendance/result/status");
}

/** 列出已同步的劳勤考勤结果。 */
export function getExternalAttendanceResult(opts: {
  dateFrom: string;
  dateTo: string;
  limit?: number;
  userCode?: string;
  userName?: string;
  deptName?: string;
}) {
  const p = new URLSearchParams();
  p.set("dateFrom", opts.dateFrom);
  p.set("dateTo", opts.dateTo);
  p.set("limit", String(opts.limit ?? 500));
  if (opts.userCode) p.set("userCode", opts.userCode);
  if (opts.userName) p.set("userName", opts.userName);
  if (opts.deptName) p.set("deptName", opts.deptName);
  return apiGet<{ dateFrom: string; dateTo: string; total: number; count: number; items: ExternalAttendanceResultRaw[] }>(
    `/admin/dingtalk-attendance/result?${p.toString()}`
  );
}

// ---- 钉钉通讯录同步（HR/admin）：后台同步部门树+员工 → 转 employee 花名册 ----
export type DingtalkEmpSyncStatus = {
  kind: string;
  status: "idle" | "running" | "done" | "error";
  phase: string | null;
  currentRoot: string | null;
  deptTotal: number;
  deptDone: number;
  memberTotalDepts: number;
  memberDoneDepts: number;
  memberCount: number;
  message: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  updatedAt: string | null;
  roots: Record<string, number>;
};

export type DingtalkEmpRaw = {
  userid: string;
  name: string | null;
  jobNumber: string | null;
  title: string | null;
  mobile: string | null;
  active: boolean | null;
  deptPath: string | null;
  rootLabel: string | null;
};

/** 非阻塞启动后台通讯录同步。 */
export function startDingtalkEmployeeSync() {
  return apiPost<{ started: boolean; alreadyRunning: boolean }>(
    "/admin/dingtalk-employee/sync",
    {}
  );
}

/** 轮询同步进度。 */
export function getDingtalkEmployeeSyncStatus() {
  return apiGet<DingtalkEmpSyncStatus>("/admin/dingtalk-employee/sync-status");
}

export type TransformResult = {
  inserted: number;
  updated: number;
  rawTotal: number;
  roleSet: number;
  postSet: number;
  regionSet: number;
  chiefBound: number;
  chiefUnmatched: string[];
  overwrite: boolean;
};

/** 原始员工 → employee 花名册（overwrite=true 时覆盖已有角色/常用职位/常用地区）。 */
export function transformDingtalkEmployees(overwrite = false) {
  return apiPost<TransformResult>("/admin/dingtalk-employee/transform", { overwrite });
}

export type ImportOneResult = {
  empId: string;
  name: string;
  role: string | null;
  title: string | null;
  action: "inserted" | "updated";
  roleSet: boolean;
  postSet: boolean;
  regionSet: boolean;
  chiefBound: boolean;
  chiefUnmatched: boolean;
  userid: string;
  deptPath: string | null;
  rootLabel: string | null;
};

/** HR 单人导入：用钉钉直接查询某个 userid 落地花名册（无需先跑整体同步）。 */
export function importOneDingtalkEmployee(userid: string, overwrite = false) {
  return apiPost<ImportOneResult>("/admin/dingtalk-employee/import-one", { userid, overwrite });
}

/** 列出已同步的原始员工。 */
export function getDingtalkEmployeeRaw(opts?: { q?: string; limit?: number }) {
  const p = new URLSearchParams();
  if (opts?.q) p.set("q", opts.q);
  p.set("limit", String(opts?.limit ?? 500));
  return apiGet<{ total: number; count: number; items: DingtalkEmpRaw[] }>(
    `/admin/dingtalk-employee/raw?${p.toString()}`
  );
}

export type EmployeeDifferenceField = {
  current: string | string[];
  remote: string | null;
  different: boolean;
  syncable: boolean;
  reason: string | null;
  protected: boolean;
  willRemove: string[];
};

export type EmployeeDifferenceItem = {
  empId: string;
  name: string;
  userid: string;
  remoteName: string | null;
  remoteTitle: string | null;
  rootLabel: string | null;
  deptPath: string | null;
  remoteSyncedAt: string | null;
  hasDifference: boolean;
  fields: Record<"region" | "role" | "post", EmployeeDifferenceField>;
};

export function getDingtalkEmployeeDifferences(opts: {
  q?: string;
  field?: "region" | "role" | "post";
  differentOnly?: boolean;
  page?: number;
  pageSize?: number;
  updateMode?: "append" | "fill_empty" | "replace";
} = {}) {
  const p = new URLSearchParams();
  if (opts.q) p.set("q", opts.q);
  if (opts.field) p.set("field", opts.field);
  p.set("differentOnly", String(opts.differentOnly ?? true));
  p.set("page", String(opts.page ?? 1));
  p.set("pageSize", String(opts.pageSize ?? 100));
  p.set("updateMode", opts.updateMode ?? "append");
  return apiGet<{
    total: number;
    page: number;
    pageSize: number;
    remoteSyncedAt: string | null;
    items: EmployeeDifferenceItem[];
  }>(`/admin/dingtalk-employee/differences?${p.toString()}`);
}

export type ApplyEmployeeDifferencesResult = {
  requested: number;
  changed: number;
  unchanged: number;
  skipped: number;
  skippedItems: { empId: string; reason: string }[];
  warnings: { empId: string; reason: string }[];
  field: "region" | "role" | "post";
};

export function applyDingtalkEmployeeDifferences(body: {
  empIds: string[];
  field: "region" | "role" | "post";
  confirmProtected?: boolean;
  // 前端透传当前更新方式；后端 apply_employee_differences 按 append/fill_empty/replace 落地。
  updateMode?: "append" | "fill_empty" | "replace";
  // 规则预览页提交整条规则，服务端写入前重算 skipMulti 等全部保护条件。
  rule?: EmployeeSyncRule;
}) {
  return apiPost<ApplyEmployeeDifferencesResult>(
    "/admin/dingtalk-employee/apply-differences",
    body,
  );
}

export type EmployeeSyncField = "region" | "role" | "post";
export type EmployeeSyncScope = "local_empty" | "different" | "mapped" | "syncable";
export type EmployeeSyncUpdateMode = "append" | "fill_empty" | "replace";
export type EmployeeDifferenceType =
  | "empty_remote"
  | "contains_remote"
  | "missing_remote"
  | "multi";

export type EmployeeSyncRule = {
  field: EmployeeSyncField;
  enabled: boolean;
  scope: EmployeeSyncScope;
  updateMode: EmployeeSyncUpdateMode;
  q: string;
  localRegions: string[];
  currentRoles: string[];
  targetRoles: string[];
  remoteTitles: string[];
  departments: string[];
  furnaces: string[];
  differenceTypes: EmployeeDifferenceType[];
  skipMulti: boolean;
};

export type EmployeeSyncPreviewItem = {
  empId: string;
  name: string;
  userid: string;
  remoteTitle: string | null;
  deptPath: string | null;
  rootLabel: string | null;
  currentRole: string;
  targetRole: string | null;
  localRegions: string[];
  field: EmployeeSyncField;
  current: string | string[];
  remote: string | null;
  different: boolean;
  differenceTypes: EmployeeDifferenceType[];
  action: "fill" | "append" | "replace" | "unchanged" | "skip";
  willAdd: string[];
  willRemove: string[];
  skipReason: string | null;
  whitelisted: boolean;
  remoteFresh: boolean;
  remoteActive: boolean;
  remoteSyncedAt: string | null;
};

export type EmployeeSyncPreview = {
  matched: number;
  willUpdate: number;
  unchanged: number;
  skipped: number;
  skipReasons: Record<string, number>;
  page: number;
  pageSize: number;
  items: EmployeeSyncPreviewItem[];
  options: Record<
    "localRegions" | "currentRoles" | "targetRoles" | "remoteTitles" | "departments" | "furnaces",
    string[]
  >;
  rule: EmployeeSyncRule;
};

export function previewDingtalkEmployeeDifferences(
  rule: EmployeeSyncRule,
  page = 1,
  pageSize = 100,
) {
  return apiPost<EmployeeSyncPreview>(
    `/admin/dingtalk-employee/differences/preview?page=${page}&pageSize=${pageSize}`,
    rule,
  );
}

export type EmployeeAutoSyncConfig = {
  enabled: boolean;
  rules: Record<EmployeeSyncField, EmployeeSyncRule>;
  updatedAt: string | null;
  updatedBy: string | null;
};

export function getEmployeeAutoSyncConfig() {
  return apiGet<EmployeeAutoSyncConfig>("/admin/dingtalk-employee/auto-sync-config");
}

export function putEmployeeAutoSyncConfig(config: Pick<EmployeeAutoSyncConfig, "enabled" | "rules">) {
  return apiPut<EmployeeAutoSyncConfig>("/admin/dingtalk-employee/auto-sync-config", config);
}

export type EmployeeSyncWhitelistItem = {
  empId: string;
  name: string;
  role: string;
  createdAt: string;
  createdBy: string;
};

export function getEmployeeSyncWhitelist(q = "") {
  const suffix = q ? `?q=${encodeURIComponent(q)}` : "";
  return apiGet<{ items: EmployeeSyncWhitelistItem[] }>(
    `/admin/dingtalk-employee/sync-whitelist${suffix}`,
  );
}

export function addEmployeeSyncWhitelist(empIds: string[]) {
  return apiPost<{ items: EmployeeSyncWhitelistItem[] }>(
    "/admin/dingtalk-employee/sync-whitelist",
    { empIds },
  );
}

export function removeEmployeeSyncWhitelist(empId: string) {
  return apiGet<{ items: EmployeeSyncWhitelistItem[] }>(
    `/admin/dingtalk-employee/sync-whitelist/${encodeURIComponent(empId)}`,
    { method: "DELETE" },
  );
}

export type EmployeeManualSyncStatus = {
  id?: number;
  trigger?: "manual" | "auto";
  actor?: string;
  status: "idle" | "running" | "done" | "error";
  phase?: string | null;
  message?: string | null;
  result?: {
    pull: { refreshedEmpIds: string[]; failedDepts: { deptId: number; deptPath: string; reason: string }[] };
    fields: {
      field: EmployeeSyncField;
      matched: number;
      changed: number;
      unchanged: number;
      skipped: number;
      skipReasons: Record<string, number>;
    }[];
  } | null;
  startedAt?: string | null;
  finishedAt?: string | null;
};

export function startEmployeeManualSync(rule: EmployeeSyncRule) {
  return apiPost<EmployeeManualSyncStatus>("/admin/dingtalk-employee/manual-sync", { rule });
}

export function getEmployeeManualSyncStatus(runId?: number) {
  const suffix = runId ? `?runId=${runId}` : "";
  return apiGet<EmployeeManualSyncStatus>(
    `/admin/dingtalk-employee/manual-sync/status${suffix}`,
  );
}

// ---- 同步范围（根部门）：描述 / 钉钉部门 ID / 基础地区，管理员可编辑 ----
export type RosterRoot = {
  label: string;
  deptId: number;
  regionCode: string | null;
  regionName: string | null;
  sort: number;
};

/** 读取同步范围列表（isDefault=true 表示仍是代码默认值，未落库）。 */
export function getRosterRoots() {
  return apiGet<{ items: RosterRoot[]; isDefault: boolean }>(
    "/admin/dingtalk-employee/roster-roots"
  );
}

/** 覆盖式保存同步范围（按数组顺序，描述/部门 ID 唯一，并选择有效基础地区）。 */
export function putRosterRoots(items: { label: string; deptId: number; regionCode: string }[]) {
  return apiPut<{ items: RosterRoot[]; isDefault: boolean }>(
    "/admin/dingtalk-employee/roster-roots",
    { items }
  );
}

// ---- 职位映射：钉钉职位 → 角色 / 常用职位 ----
export type TitleMap = { title: string; role: string; post: string; sort: number };

/** 读取职位映射列表。 */
export function getTitleMap() {
  return apiGet<{ items: TitleMap[] }>("/admin/dingtalk-employee/title-map");
}

/** 覆盖式保存职位映射（title 唯一；role/post 可空）。 */
export function putTitleMap(items: { title: string; role: string; post: string }[]) {
  return apiPut<{ items: TitleMap[] }>("/admin/dingtalk-employee/title-map", { items });
}

/** 已同步但未映射的去重职位（含人数），供一键导入。 */
export function getUnmappedTitles() {
  return apiGet<{ items: { title: string; count: number }[] }>(
    "/admin/dingtalk-employee/unmapped-titles"
  );
}

// ---- 推送调试（超管）：工作点→值班长→钉钉号状态 + 测试推送 ----
export type FurnacePushStatus = {
  furnaceId: number;
  region: string;
  furnaceNo: string;
  hasChief: boolean;
  chiefs: { empId: string; name: string; ddUserId: string; hasDing: boolean }[];
};
export function getFurnacePushStatus() {
  return apiGet<{ items: FurnacePushStatus[] }>("/admin/shift-push/furnaces");
}
export function sendTestPush(empId: string) {
  return apiPost<{ ok: boolean; error?: string; taskId?: number | string | null }>(
    "/admin/shift-push/test",
    { empId }
  );
}
export type PushLog = {
  id: number;
  ts: string | null;
  kind: string; // shift_push/test_push/上岗提醒/缺编提醒/当日小结/通知…
  status: string; // "sent" | "failed" | "skipped"
  target?: string | null; // 接收人工号
  targetName?: string | null; // 接收人姓名
  subject?: string | null; // 推送标题
  timePoint?: string | null;
  date?: string | null;
  region?: string | null;
  furnace?: string | null;
  squad?: string | null;
  chiefEmpId?: string | null;
  chiefName?: string | null;
  ddUserId?: string | null;
  reason?: string;
  text?: string | null;
  result?: {
    ok?: boolean;
    error?: string;
    taskId?: number | string | null;
    forbidden?: { userid: string; code: string; reason: string }[];
    invalidUsers?: string[];
    raw?: unknown;
  } | null;
};
export function getPushLogs(filters?: { kind?: string; date?: string; q?: string }) {
  const params = new URLSearchParams();
  if (filters?.kind) params.set("kind", filters.kind);
  if (filters?.date) params.set("date", filters.date);
  if (filters?.q) params.set("q", filters.q);
  const qs = params.toString();
  return apiGet<{ items: PushLog[]; kinds: string[] }>(
    `/admin/shift-push/logs${qs ? `?${qs}` : ""}`
  );
}

// ---- 连接池监控（管理员）----
export type ConnectionPoolSnapshot = {
  supported: boolean;
  poolClass: string;
  reason?: string;
  poolSize?: number;
  maxOverflow?: number;
  maxCapacity?: number;
  checkedIn?: number;
  checkedOut?: number;
  currentOverflow?: number;
  establishedConnections?: number;
  utilizationPercent?: number;
  timeoutSeconds?: number | null;
  statusText?: string;
  includesObserverRequest?: boolean;
};

export type DatabaseConnectionSnapshot = {
  supported: boolean;
  backend: string;
  reason?: string;
  maxConnections?: number;
  reservedConnections?: number;
  availableBudget?: number;
  remainingConnections?: number;
  serverTotal?: number;
  databaseTotal?: number;
  active?: number;
  idle?: number;
  idleInTransaction?: number;
  waiting?: number;
  lockWaiting?: number;
  longestTransactionSeconds?: number;
  utilizationPercent?: number;
  includesObserverRequest?: boolean;
};

export type RequestAuditQueueSnapshot = {
  enabled: boolean;
  available: boolean | null;
  acceptedSinceStart: number;
  enqueuedSinceStart: number;
  failedSinceStart: number;
  overflowedSinceStart: number;
  byteOverflowedSinceStart: number;
  redisUnavailableFallbacksSinceStart: number;
  redisWriteFailuresSinceStart: number;
  redisRetryEventsSinceStart: number;
  unavailableDurationSeconds: number;
  memoryQueue: {
    capacity: number;
    capacityBytes: number;
    waiting: number;
    inFlight: number;
    pending: number;
    utilizationPercent: number;
    itemUtilizationPercent: number;
    byteUtilizationPercent: number;
    serializedBytes: number;
    oldestEventAgeMs: number;
    writerCount: number;
    batchSize: number;
  };
  streamHealth?: {
    length: number | null;
    pending: number | null;
    consumerLag: number | null;
    workerHeartbeatAgeSeconds: number | null;
    workerHealthy: boolean;
    maxLength: number;
    sampledAt: string;
    probeError: string | null;
  } | null;
  lastError?: string | null;
};

export type ConnectionPoolStatus = {
  generatedAt: string;
  status: "healthy" | "warning" | "critical" | "unavailable";
  process: {
    pid: number;
    databaseBackend: string;
    scope: "current_process";
  };
  pool: ConnectionPoolSnapshot;
  database: DatabaseConnectionSnapshot;
  requestAuditQueue?: RequestAuditQueueSnapshot;
  reports?: {
    scope: "current_process";
    paused: boolean;
    capacity: number;
    active: number;
    inFlight: { endpoint: string; seconds: number; followers: number }[];
    completed: number;
    failed: number;
    rejected: number;
    coalesced: number;
    recentCount: number;
    recentP95Ms: number | null;
    timeoutSeconds: number;
    maxSourceRows: number;
    maxOutputCells: number;
    residentMemoryMb: number | null;
  };
  thresholds: {
    poolWarningPercent: number;
    poolCriticalPercent: number;
    databaseWarningPercent: number;
    databaseCriticalPercent: number;
  };
  notes: string[];
};

export function getConnectionPoolStatus() {
  return apiGet<ConnectionPoolStatus>("/admin/connection-pool");
}

export type DingtalkUserFetchLoadTestResultItem = {
  index: number;
  userid: string;
  ok: boolean;
  latencyMs: number;
  errcode: number | string | null;
  error: string | null;
  rateLimited: boolean;
  name: string | null;
  title: string | null;
  departmentCount: number | null;
};

export type DingtalkUserFetchLoadTestResult = {
  generatedAt: string;
  mode: "read_only_user_get";
  requestCount: number;
  configuredConcurrency: number;
  peakConcurrency: number;
  useridCount: number;
  durationMs: number;
  throughputRps: number;
  successCount: number;
  errorCount: number;
  successRate: number;
  rateLimitedCount: number;
  latencyMs: {
    min: number;
    average: number;
    p50: number;
    p95: number;
    p99: number;
    max: number;
  };
  results: DingtalkUserFetchLoadTestResultItem[];
  notes: string[];
};

/** 管理员只读压测钉钉 user/get；不会写员工、收藏或工作点绑定数据。 */
export function runDingtalkUserFetchLoadTest(body: {
  userids: string[];
  requestCount: number;
  concurrency: number;
  confirmHighLoad?: boolean;
}) {
  return apiPost<DingtalkUserFetchLoadTestResult>(
    "/admin/dingtalk-user-fetch-load-test",
    body,
  );
}

export type SyncExecutionLog = {
  id: number; kind: string; kindLabel: string; trigger: "auto" | "manual";
  actor: string; region: string | null; dateFrom: string | null; dateTo: string | null;
  status: "running" | "success" | "partial" | "failed" | "skipped";
  startedAt: string; finishedAt: string | null; counts: Record<string, number>;
  fetched: number; upserted: number; message: string | null;
};
export type SyncExecutionLogs = {
  items: SyncExecutionLog[]; total: number; page: number; pageSize: number;
  kinds: Record<string, string>; countLabels: Record<string, string>;
};
export function getSyncExecutionLogs(params: {
  group: "attendance" | "employee"; page?: number; pageSize?: number;
  kind?: string; trigger?: string; status?: string; dateFrom?: string; dateTo?: string;
}): Promise<SyncExecutionLogs> {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") query.set(key, String(value));
  });
  return apiGet<SyncExecutionLogs>(`/admin/sync-execution-logs?${query}`);
}
