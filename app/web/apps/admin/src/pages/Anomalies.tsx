import SyncLogButton from "../components/SyncLogButton";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Drawer,
  Empty,
  Input,
  List,
  Modal,
  Radio,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
const { RangePicker } = DatePicker;
type DateRange = [Dayjs | null, Dayjs | null] | null;
import { useTableColumns } from "../useTableColumns";
import { registerVolatilePageCache, usePageCache } from "../pageCache";
import {
  apiGet,
  apiPost,
  getAdminEmployees,
  getFurnaces,
  getMe,
  getPageRegionScope,
  getReconTimeline,
  isHalfShift,
  workHoursLabel,
  type AdminEmployee,
  type FurnaceItem,
  type ReconTimeline,
} from "@fdc/api-client";
import {
  HourRuler,
  OvertimeCheckLine,
  TimelineAxis,
  TimelineLegend,
} from "../components/ReconTimeline";

type Row = {
  rowKey?: string;
  rowKind?: "recon" | "overtime";
  overtimeId?: number;
  empId: string;
  name?: string;
  status: string;
  furnace: string;
  region?: string;
  delegateFurnaceId?: number | null;
  delegateRegion?: string;
  delegateFurnace?: string;
  delegateOutOfScope?: boolean;
  delegateChiefName?: string;
  delegateChiefEmpId?: string;
  delegateChiefDdUserId?: string;
  chiefCanConfirm?: boolean;
  date: string;
  chiefName?: string;
  furnaceChiefs?: { name: string; empId: string }[];
  chiefEmpId?: string;
  chiefDdUserId?: string;
  boundChief?: { name: string; empId: string; ddUserId: string } | null;
  dataSource?: string;
  hrStatus: string;
  chiefStatus: string;
  chiefStatusDisplay: string;
  pendingMakeup?: boolean;
  hasScan?: boolean;
  laoqinCount?: number;
  workHours?: number;
  overtimeHours?: number;
  attendanceResultWorkHours?: string | null;
  post?: string;
  squad?: string | null;
  shiftSlot?: string | null;
  formalHours?: number;
  internHours?: number;
  standbyHours?: number;
  furnaces?: { region: string; furnace: string }[];
  regionPending?: boolean;
  valid: boolean;
  validReason: string;
  validKind?: string;
};

type Options = {
  regions: string[];
  furnaces: string[];
  chiefs: string[];
  statuses: string[];
  confirmStates: string[];
  hrConfirmStates: string[];
  chiefConfirmStates: string[];
  dataSources: string[];
  scanData: string[];
  laoqinBuckets: string[];
  validKinds: string[];
};

type Evidence = {
  date: string;
  region: string;
  furnace: string;
  status: string;
  attendanceResultWorkHours?: string | null;
  laoqinPunches: { id: number; naturalDate: string; time: string; inoutType: string; source: string | null; makeupReason?: string | null }[];
  scanRecords: {
    id: number; naturalDate: string; time: string; region: string; furnace: string; post: string;
    squad: string | null; employType: string; source: string; workHours?: number | null;
  }[];
  makeups: {
    id: number; naturalDate: string; region: string; furnace: string; squad: string | null; post: string;
    employType: string; kind: string; status: string; reason: string | null; approver?: string | null;
  }[];
  overtimes?: {
    id: number; naturalDate: string; region: string; furnace: string; squad: string | null; post: string;
    employType: string; workHours: number; reconDingtalk: boolean; approver?: string | null;
    status: string; valid: boolean; validReason: string;
  }[];
};

const STATUS_COLOR: Record<string, string> = {
  自动一致: "green", 时间错位: "orange", 工作点有劳勤无: "red", 劳勤有工作点无: "volcano",
  加班需核对: "purple",
};
const CONFIRM_COLOR: Record<string, string> = {
  通过: "green", 不通过: "red", 待值班长确认: "blue", 补录待值班长确认: "gold", 未确认: "default",
};
const STATE_EXPLAIN: Record<string, string> = {
  通过: "直接判该班次有效。",
  不通过: "判该班次无效。",
  未确认: "撤销确认，恢复为按问题/自动判定。",
};

/** 状态说明（「待值班长确认」需带上扫码工作点值班长的名字，区别于弹窗里的常用值班长）。 */
function explainState(
  state: string,
  row?: {
    delegateChiefName?: string;
    delegateChiefEmpId?: string;
    delegateFurnaceId?: number | null;
    delegateRegion?: string;
    delegateOutOfScope?: boolean;
  },
): string {
  if (state === "待值班长确认") {
    if (row?.delegateFurnaceId === undefined && row?.delegateOutOfScope === undefined) {
      return "数据版本较旧，请刷新工作台后重试。";
    }
    if (row?.delegateOutOfScope) {
      return "员工当日委托工作点不在您的管辖地区，请联系对应地区 HR 处理。";
    }
    if (!row?.delegateFurnaceId) {
      return "未能定位员工当日基础出勤工作点，请先修复考勤归属后再委托。";
    }
    if (!row.delegateRegion) {
      return "无法确定委托工作点所属地区，请联系管理员检查工作点配置。";
    }
    return row?.delegateChiefName
      ? `推送给委托目标工作点值班长 ${row.delegateChiefName}（${row.delegateChiefEmpId || "—"}）确认；其确认通过后该班次即有效，部门统计无需再确认。`
      : "该工作点未绑定值班长，请在下方指定；指定后推送其确认，通过即生效（部门统计无需再确认）。";
  }
  return STATE_EXPLAIN[state] ?? "";
}

function fmtHours(v: number | string | null | undefined): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (Number.isFinite(n)) return `${Number(n.toFixed(2))}小时`;
  return `${v}小时`;
}

type Filters = Partial<Record<
  "region" | "furnace" | "name" | "chiefName" | "status" | "dataSource" | "hrStatus" | "chiefStatus" | "scanData" | "laoqin" | "validKind",
  string
>>;

const fmtTime = (iso: string) => iso.slice(11, 19);
// 证据链每条记录的自然日：与抽屉查询日一致则灰色，不一致则红色加粗（提示该补录/扫码其实归属别的日子）。
const srcDate = (nd: string, queryDate: string) =>
  nd === queryDate
    ? <span style={{ color: "#bbb", marginRight: 4 }}>{nd} · </span>
    : <span style={{ color: "#cf1322", fontWeight: 600, marginRight: 4 }}>{nd}⚠ · </span>;

const DATE_RANGE_KEY = "fdc.anomalies.dateRange";

/** 从 hash 深链 #/anomalies?date=YYYY-MM-DD 读取日期范围（考勤对照等跳转会带 date，命中则范围为该单日）。 */
function hashRange(): DateRange {
  const hash = window.location.hash;
  const qi = hash.indexOf("?");
  if (qi >= 0) {
    const d = new URLSearchParams(hash.slice(qi + 1)).get("date");
    if (d && /^\d{4}-\d{2}-\d{2}$/.test(d)) return [dayjs(d), dayjs(d)];
  }
  return null;
}

function storedRange(): DateRange {
  try {
    const raw = localStorage.getItem(DATE_RANGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { from?: string; to?: string };
    if (!parsed.from || !parsed.to) return null;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(parsed.from) || !/^\d{4}-\d{2}-\d{2}$/.test(parsed.to)) return null;
    return [dayjs(parsed.from), dayjs(parsed.to)];
  } catch {
    return null;
  }
}

/** 初始日期优先级：hash 深链 > 本地暂存 > 本月首日～本月末日。 */
function initialRange(): DateRange {
  return hashRange() ?? storedRange() ?? monthRange(dayjs());
}

function monthRange(month: Dayjs): DateRange {
  return [month.startOf("month"), month.endOf("month")];
}

const rangeKey = (r: DateRange) =>
  `${r?.[0]?.format("YYYY-MM-DD") ?? ""}~${r?.[1]?.format("YYYY-MM-DD") ?? ""}`;

// 行内「考勤对照」时间轴：展开某行时按 该员工+该日 懒加载 recon 时间轴并缓存
const tlCache = new Map<string, ReconTimeline>();
registerVolatilePageCache("/anomalies", () => tlCache.clear());

function RowTimeline({ empId, date, overtimeId }: { empId: string; date: string; overtimeId?: number }) {
  const key = `${empId}|${date}`;
  const [tl, setTl] = useState<ReconTimeline | null>(() => tlCache.get(key) ?? null);
  const [loading, setLoading] = useState(!tlCache.has(key));
  const [err, setErr] = useState(false);

  useEffect(() => {
    if (tlCache.has(key)) { setTl(tlCache.get(key)!); setLoading(false); return; }
    setLoading(true);
    setErr(false);
    getReconTimeline({ date, q: empId, region: getPageRegionScope() })
      .then((r) => { tlCache.set(key, r); setTl(r); })
      .catch(() => setErr(true))
      .finally(() => setLoading(false));
  }, [key, empId, date]);

  if (loading) return <div style={{ padding: 16 }}><Spin /></div>;
  if (err || !tl) return <Empty style={{ padding: 16 }} description="加载考勤对照失败" />;
  const item = tl.items.find((it) => it.empId === empId);
  if (!item) return <Empty style={{ padding: 16 }} description="该员工当日无扫码 / 钉钉数据" />;
  const overtime = item.overtimes?.find((row) => row.id === overtimeId);
  const region = item.scans[0]?.region ?? item.pendingMakeups?.[0]?.region;
  const windows =
    (region && tl.squadSchedules.find((s) => s.region === region)?.squads) ||
    tl.squadSchedules[0]?.squads ||
    [];
  const fs = item.scans[0] ?? item.pendingMakeups?.[0];
  const place = fs ? `${fs.region ?? ""} ${fs.furnace ?? ""}`.trim() : "—";

  return (
    <div style={{ background: "#fafafa", borderRadius: 6, padding: "8px 4px" }}>
      {overtime && <OvertimeCheckLine overtime={overtime} />}
      <div style={{ marginBottom: 6 }}><TimelineLegend /></div>
      {/* 刻度尺 + 时间轴：左侧 210px 信息列与 AttendanceCompare 对齐 */}
      <div style={{ display: "flex" }}>
        <div style={{ width: 210, flexShrink: 0 }} />
        <div style={{ flex: 1, paddingTop: 2 }}><HourRuler /></div>
      </div>
      <div style={{ display: "flex", alignItems: "stretch", border: "1px solid #f0f0f0", borderRadius: 4, background: "#fff" }}>
        <div style={{ width: 210, flexShrink: 0, padding: "10px 12px" }}>
          <div style={{ fontWeight: 600 }}>{item.name}</div>
          <div style={{ fontSize: 12, color: "#666", marginTop: 2 }}>{place}</div>
        </div>
        <TimelineAxis item={item} windows={windows} date={tl.date} />
      </div>
    </div>
  );
}

const EXPAND_ALL_KEY = "fdc.anomalies.expandAllCompare";
const PENDING_ONLY_KEY = "fdc.anomalies.pendingOnly"; // 默认只看待处理(自动无效)，记忆到本地
const PENDING_VALID_KIND = "自动无效";
const DEPARTMENT_CONFIRM_VISIBLE_MIGRATION_KEY = "fdc.anomalies.departmentConfirmVisible.v1";
const rowKeyOf = (r: Row) => r.rowKey ?? `${r.empId}-${r.date}`;

/** 新名称上线时一次性打开“部门确认”列；之后仍尊重用户手动显示/隐藏的选择。 */
function migrateDepartmentConfirmVisible() {
  try {
    if (localStorage.getItem(DEPARTMENT_CONFIRM_VISIBLE_MIGRATION_KEY) === "1") return;
    const storageKey = "tablecols:anomalies3";
    const raw = localStorage.getItem(storageKey);
    if (raw) {
      const prefs = JSON.parse(raw) as { hidden?: string[]; widths?: Record<string, number> };
      if (Array.isArray(prefs.hidden)) {
        prefs.hidden = prefs.hidden.filter((key) => key !== "hr");
        localStorage.setItem(storageKey, JSON.stringify(prefs));
      }
    }
    localStorage.setItem(DEPARTMENT_CONFIRM_VISIBLE_MIGRATION_KEY, "1");
  } catch {
    /* ignore */
  }
}

export default function AnomaliesPage({ active = true }: { active?: boolean }) {
  const [range, setRange] = useState<DateRange>(initialRange);
  const [quickMonth, setQuickMonth] = useState<Dayjs | null>(() => range?.[0] ?? dayjs());
  // 进入默认只看「待处理」(自动无效)，可切换全部显示，记忆到本地
  const [pendingOnly, setPendingOnly] = useState(() => localStorage.getItem(PENDING_ONLY_KEY) !== "0");
  const [filters, setFilters] = useState<Filters>(() =>
    localStorage.getItem(PENDING_ONLY_KEY) !== "0" ? { validKind: PENDING_VALID_KIND } : {}
  );
  const togglePending = () => {
    setPendingOnly((prev) => {
      const next = !prev;
      localStorage.setItem(PENDING_ONLY_KEY, next ? "1" : "0");
      setFilters((f) => ({ ...f, validKind: next ? PENDING_VALID_KIND : undefined }));
      return next;
    });
  };
  // 姓名/值班长名字仅作为输入草稿；点击“搜索”或按回车后才写入 filters 触发查询。
  const [nameInput, setNameInput] = useState("");
  const [chiefInput, setChiefInput] = useState("");
  const [refreshSeq, setRefreshSeq] = useState(0);
  const applyTextFilters = useCallback(() => {
    const nextName = nameInput.trim() || undefined;
    const nextChiefName = chiefInput.trim() || undefined;
    setFilters((f) => {
      if (f.name === nextName && f.chiefName === nextChiefName) return f;
      return { ...f, name: nextName, chiefName: nextChiefName };
    });
  }, [nameInput, chiefInput]);
  const [data, setData] = useState<{ items: Row[]; options: Options; total: number; count: number; page: number; pageSize: number; pendingCount?: number } | null>(null);
  const [loadError, setLoadError] = useState<{ status?: number; detail: string } | null>(null);
  const [availableFurnaces, setAvailableFurnaces] = useState<FurnaceItem[]>([]);
  const [page, setPage] = useState(1);
  const pageSize = 50;
  const [loading, setLoading] = useState(false);
  const loadingRef = useRef(false);
  const previousActive = useRef(active);
  const { invalidate } = usePageCache();
  const [role, setRole] = useState("");
  const [employees, setEmployees] = useState<AdminEmployee[]>([]);
  const [drawer, setDrawer] = useState<Row | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  // 行内考勤对照展开状态（受控）；expandAll 记忆「一键全展开」偏好到 localStorage
  const [expandedKeys, setExpandedKeys] = useState<readonly string[]>([]);
  const [expandAll, setExpandAll] = useState(() => localStorage.getItem(EXPAND_ALL_KEY) === "1");

  // 确认弹窗
  const [cm, setCm] = useState<{
    row: Row;
    kind: "hr" | "chief";
    needChiefOverride: boolean;
    needChiefHint: string;
  } | null>(null);
  const [cmEvidence, setCmEvidence] = useState<Evidence | null>(null);
  const [pick, setPick] = useState<string | undefined>();
  const [pickedChief, setPickedChief] = useState<string | undefined>();
  const [submitting, setSubmitting] = useState(false);
  const [chiefCandidatesState, setChiefCandidatesState] = useState<
    "idle" | "loading" | "ready" | "error" | "incompatible"
  >("idle");
  const loadChiefCandidates = useCallback(() => {
    setEmployees([]);
    setChiefCandidatesState("loading");
    getAdminEmployees({ employmentStatus: "active" })
      .then((r) => {
        const contractMissing = r.items.some(
          (item) =>
            !("explicitRole" in item)
            || !Array.isArray(item.chiefRegions)
            || typeof item.chiefCandidateEligible !== "boolean",
        );
        if (r.candidateContractVersion !== 2 || contractMissing) {
          setEmployees([]);
          setChiefCandidatesState("incompatible");
          return;
        }
        setEmployees(r.items);
        setChiefCandidatesState("ready");
      })
      .catch(() => {
        setEmployees([]);
        setChiefCandidatesState("error");
      });
  }, []);
  useEffect(() => {
    if (active) return;
    setDrawer(null);
    setEvidence(null);
    setCm(null);
    setCmEvidence(null);
    setPick(undefined);
    setPickedChief(undefined);
  }, [active]);
  // SPA 内 hash 二次切换(如再次带 ?date= 跳转)时同步日期范围，不止首次 mount
  useEffect(() => {
    const onHash = () => {
      const r = hashRange();
      if (!r) return;
      setRange((prev) => {
        if (rangeKey(prev) === rangeKey(r)) return prev;
        setQuickMonth(r?.[0] ?? null);
        return r;
      });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    getMe().then((m) => setRole(m.role)).catch(() => undefined);
    // 取员工含钉钉号（HR 确认弹窗选值班长时展示 姓名/工号/钉钉号）；stat/hr/admin 可读，其余角色 403 忽略即可
    loadChiefCandidates();
  }, [loadChiefCandidates]);

  useEffect(() => {
    if (!active) return;
    let alive = true;
    getFurnaces()
      .then((result) => { if (alive) setAvailableFurnaces(result.items); })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [active]);

  const fromStr = range?.[0] ? range[0].format("YYYY-MM-DD") : "";
  const toStr = range?.[1] ? range[1].format("YYYY-MM-DD") : "";
  useEffect(() => {
    if (fromStr && toStr) {
      localStorage.setItem(DATE_RANGE_KEY, JSON.stringify({ from: fromStr, to: toStr }));
    } else {
      localStorage.removeItem(DATE_RANGE_KEY);
    }
  }, [fromStr, toStr]);
  const applyQuickMonth = (month: Dayjs | null) => {
    if (loadingRef.current) return;
    setQuickMonth(month);
    setRange(month ? monthRange(month) : null);
  };
  const refreshCurrentSearch = () => {
    if (loadingRef.current) return;
    applyTextFilters();
    // 显式失效本页 sessionStorage 缓存：筛选条件不变时点「搜索」也必须回源，避免命中旧响应。
    invalidate("/anomalies");
    setPage(1);
    setExpandedKeys([]);
    setRefreshSeq((v) => v + 1);
  };
  const load = useCallback(() => {
    if (loadingRef.current) return;
    if (!fromStr && !toStr) {
      setData(null);
      setLoadError(null);
      setLoading(false);
      message.warning("请先选择日期范围");
      return;
    }
    const qs = new URLSearchParams();
    // 区间查询：必须带日期范围；同一天=单日
    if (fromStr) qs.set("dateFrom", fromStr);
    if (toStr) qs.set("dateTo", toStr);
    const scopedRegion = getPageRegionScope();
    if (scopedRegion) qs.set("region", scopedRegion);
    qs.set("page", String(page));
    qs.set("pageSize", String(pageSize));
    for (const [k, v] of Object.entries(filters)) if (v) qs.set(k, v);
    loadingRef.current = true;
    setLoading(true);
    setLoadError(null);
    apiGet<{ items: Row[]; options: Options; total: number; count: number; page: number; pageSize: number; pendingCount?: number }>(`/anomalies?${qs}`)
      .then((value) => {
        setData(value);
        setLoadError(null);
      })
      .catch((error: unknown) => {
        setData(null);
        const status = (error as { status?: number })?.status;
        setLoadError({
          status,
          detail: error instanceof Error ? error.message : "加载劳勤工作台失败，请稍后重试",
        });
      })
      .finally(() => {
        loadingRef.current = false;
        setLoading(false);
      });
  }, [fromStr, toStr, filters, page, refreshSeq]);
  useEffect(() => {
    setPage(1);
    setExpandedKeys([]);
  }, [fromStr, toStr, filters]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) {
      invalidate("/anomalies");
      setExpandedKeys([]);
      setRefreshSeq((value) => value + 1);
    }
  }, [active, invalidate]);

  // 单人定点重拉：同步该员工「这一天 ±1 天」的劳勤打卡、当天出勤工时并重跑对账，成功后刷新列表
  const [resyncing, setResyncing] = useState<string | null>(null);
  type BulkState = {
    id?: string; status: string; dateFrom?: string; dateTo?: string; region?: string;
    total?: number; done?: number; busy?: boolean; error?: string;
    counts?: Record<string, number>;
    items?: { empId: string; date: string; status: string; attempts: number; error?: string;
      before?: { rows: { status: string; attendanceResultWorkHours?: string }[] };
      after?: { rows: { status: string; attendanceResultWorkHours?: string }[] } }[];
  };
  const [bulk, setBulk] = useState<BulkState>({ status: "idle" });
  const [bulkStarting, setBulkStarting] = useState(false);
  const [bulkDetails, setBulkDetails] = useState(false);
  const bulkRunning = !!bulk.busy && ["preparing", "pending", "running"].includes(bulk.status);
  const refreshedBulk = useRef<string>();
  const bulkScope = getPageRegionScope() || filters.region || undefined;
  useEffect(() => {
    if (!active || !["admin", "hr", "stat"].includes(role)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const qs = new URLSearchParams();
        if (bulkScope) qs.set("region", bulkScope);
        const next = await apiGet<BulkState>(`/admin/dingtalk-attendance/resync-bulk-status?${qs}`, { cache: "no-store" });
        if (cancelled) return;
        setBulk(next);
        if (next.id && next.status === "done" && refreshedBulk.current !== next.id) {
          refreshedBulk.current = next.id;
          invalidate("/anomalies");
          setExpandedKeys([]);
          setRefreshSeq((n) => n + 1);
        }
      } catch {
        // 保留最后进度；短暂网络中断不把运行中的任务伪装成结束。
      } finally {
        if (!cancelled) timer = setTimeout(poll, 3000);
      }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [active, role, bulkScope, invalidate, bulkStarting]);
  const startBulk = () => {
    Modal.confirm({
      title: "批量重拉待处理异常",
      content: `将处理 ${fromStr} 至 ${toStr}、${bulkScope || "全部管理地区"}的待处理异常（不限当前页，不按姓名等列表筛选）。已封账或已人工处理的记录会跳过；中断的同范围任务会继续执行。`,
      okText: "开始重拉",
      onOk: async () => {
        setBulkStarting(true);
        try {
          const next = await apiPost<BulkState>("/admin/dingtalk-attendance/resync-bulk", {
            dateFrom: fromStr, dateTo: toStr, region: bulkScope || undefined,
          });
          setBulk(next);
          setBulkDetails(true);
          message.success("批量重拉已启动，可以离开页面，任务会在后台继续执行");
        } catch (e) {
          message.error((e as Error).message || "启动批量重拉失败");
        } finally { setBulkStarting(false); }
      },
    });
  };
  const resyncOne = useCallback((r: Row) => {
    const k = rowKeyOf(r);
    setResyncing(k);
    apiPost<{ synced?: { upserted?: number }; attendanceResult?: { synced?: { upserted?: number } }; dateFrom: string; dateTo: string }>(
      "/admin/dingtalk-attendance/resync-one",
      { empId: r.empId, date: r.date, window: 1 },
    )
      .then((res) => {
        // POST 会清模块缓存；同时收起旧 RowTimeline 实例，确保再次展开必定取新证据。
        setExpandedKeys((keys) => keys.filter((key) => key !== k));
        message.success(
          `已重拉 ${r.name || r.empId} ${res.dateFrom}~${res.dateTo}`
          + `（打卡 ${res.synced?.upserted ?? 0} 条，出勤工时 ${res.attendanceResult?.synced?.upserted ?? 0} 条），已重新对账`,
          5,
        );
        load();
      })
      .catch((e) => message.error((e as Error).message || "重拉劳勤失败（仅内网可同步考勤源）"))
      .finally(() => setResyncing(null));
  }, [load]);

  // 证据链抽屉
  useEffect(() => {
    if (!drawer) { setEvidence(null); return; }
    apiGet<Evidence>(`/anomalies/evidence?empId=${drawer.empId}&date=${drawer.date}`).then(setEvidence);
  }, [drawer]);

  const hrEditable = role === "stat" || role === "hr" || role === "admin";
  const chiefEditable = role === "chief" || role === "admin";

  // 列显示开关 + 列宽拖拽（持久化到 localStorage）
  migrateDepartmentConfirmVisible();
  const tc = useTableColumns("anomalies3", [
    { key: "date", title: "日期" },
    { key: "region", title: "地区" },
    { key: "furnace", title: "出勤工作点", defaultHidden: true },
    { key: "name", title: "姓名" },
    { key: "chiefName", title: "出勤工作点的值班长", defaultHidden: true },
    { key: "dataSource", title: "数据来源" },
    { key: "formalHours", title: "正式工时" },
    { key: "internHours", title: "实习工时" },
    { key: "standbyHours", title: "待岗工时" },
    { key: "status", title: "对账状态" },
    { key: "attendanceResultWorkHours", title: "出勤工时" },
    { key: "hr", title: "部门确认" },
    { key: "chief", title: "值班长确认" },
    { key: "valid", title: "是否可用" },
    { key: "validReason", title: "确认理由" },
    { key: "ops", title: "操作" },
  ]);

  // 打开确认弹窗：取详情 + 默认选中当前状态
  const openConfirm = (row: Row, kind: "hr" | "chief") => {
    setPick(undefined);
    setPickedChief(undefined);
    setCmEvidence(null);
    setCm({ row, kind, needChiefOverride: false, needChiefHint: "" });
    loadChiefCandidates();
    apiGet<Evidence>(`/anomalies/evidence?empId=${row.empId}&date=${row.date}`).then(setCmEvidence);
  };

  const doConfirm = async (statusOverride?: string, chiefEmpId?: string) => {
    if (!cm) return;
    const status = statusOverride ?? pick;
    if (!status) { message.warning("请选择状态"); return; }
    if (cm.kind === "hr" && status === "待值班长确认" && cm.row.delegateOutOfScope) {
      message.warning("员工当日委托工作点不在您的管辖地区，请联系对应地区 HR 处理");
      return;
    }
    if (cm.kind === "hr" && status === "待值班长确认" && !cm.row.delegateFurnaceId) {
      message.warning("未能定位员工当日基础出勤工作点，请先修复考勤归属");
      return;
    }
    if (cm.kind === "hr" && status === "待值班长确认" && !cm.row.delegateRegion) {
      message.warning("无法确定委托工作点所属地区，请联系管理员检查工作点配置");
      return;
    }
    // HR「待值班长确认」且该工作点无值班长 → 必须指定值班长
    if (
      cm.kind === "hr"
      && status === "待值班长确认"
      && (!cm.row.delegateChiefName || cm.needChiefOverride)
      && !chiefEmpId
    ) {
      message.warning("该工作点无值班长，请先指定一名值班长");
      return;
    }
    setSubmitting(true);
    try {
      const res = await apiPost<{ chiefNotified?: boolean; needChief?: boolean; chiefHint?: string }>(
        "/anomalies/confirm",
        {
          empId: cm.row.empId,
          date: cm.row.date,
          kind: cm.kind,
          status,
          chiefEmpId,
          furnaceId: status === "待值班长确认" ? cm.row.delegateFurnaceId : undefined,
        }
      );
      if (res.needChief) {
        message.warning(res.chiefHint || "该工作点无值班长，请指定");
        setCm((current) => (
          current
          && current.row.empId === cm.row.empId
          && current.row.date === cm.row.date
            ? {
              ...current,
              needChiefOverride: true,
              needChiefHint: res.chiefHint || "该工作点无在职值班长，请指定同地区值班长",
            }
            : current
        ));
        setPickedChief(undefined);
        loadChiefCandidates();
        setSubmitting(false);
        return;
      }
      // 推送值班长失败/无钉钉号 → 二次确认是否保留
      if (status === "待值班长确认" && res.chiefNotified === false) {
        setSubmitting(false);
        Modal.confirm({
          title: "值班长推送失败",
          content: `${res.chiefHint || "值班长未收到推送"}。是否仍保留「待值班长确认」？（请线下通知值班长）`,
          okText: "保留",
          cancelText: "撤销",
          onCancel: async () => {
            await apiPost("/anomalies/confirm", {
              empId: cm.row.empId, date: cm.row.date, kind: "hr", status: "未确认",
            });
            message.info("已撤销");
            setCm(null); load();
          },
          onOk: () => { message.success("已保留，请线下通知值班长"); setCm(null); load(); },
        });
        return;
      }
      message.success("已确认");
      setCm(null);
      load();
    } catch (e: unknown) {
      const err = e as { status?: number; data?: { detail?: string; error?: string } };
      if (err.status === 409) {
        message.warning(err.data?.detail || "委托目标已变化，请刷新后重试");
        setCm(null);
        setPick(undefined);
        setPickedChief(undefined);
        load();
        return;
      }
      message.error(err.status === 403 ? "无权限" : err.data?.error || err.data?.detail || "确认失败");
    } finally {
      setSubmitting(false);
    }
  };

  const opt = data?.options;
  const selectedRegion = getPageRegionScope() || filters.region;
  const fallbackFurnaceOptions = Array.from(new Set(
    availableFurnaces
      .filter((furnace) => !selectedRegion || furnace.region === selectedRegion)
      .map((furnace) => furnace.furnaceNo),
  ));
  const sel = (placeholder: string, key: keyof Filters, list: string[] | undefined) => (
    <Select allowClear placeholder={placeholder} style={{ width: 128 }} value={filters[key]}
      disabled={loading}
      onChange={(v) => setFilters((f) => ({ ...f, [key]: v }))}
      options={(list ?? []).map((x) => ({ label: x, value: x }))} />
  );

  const items = useMemo(() => data?.items ?? [], [data?.items]);
  const comparableItems = useMemo(
    () => items.filter((x) => x.rowKind === "overtime"),
    [items],
  );

  // 开启「全展开」记忆时，数据变化后自动展开当前全部行
  useEffect(() => {
    if (expandAll) setExpandedKeys(comparableItems.map(rowKeyOf));
  }, [expandAll, comparableItems]);

  const toggleExpandAll = () => {
    const next = !expandAll;
    setExpandAll(next);
    localStorage.setItem(EXPAND_ALL_KEY, next ? "1" : "0");
    setExpandedKeys(next ? comparableItems.map(rowKeyOf) : []);
  };

  const targetChiefRegion =
    cm?.row.delegateRegion || undefined;
  const chiefOptions = employees
    .filter(
      (e) =>
        e.chiefCandidateEligible === true
        && e.employmentStatus === "active"
        && !!targetChiefRegion
        && e.chiefRegions?.includes(targetChiefRegion),
    )
    .map((e) => ({
      label:
        `${e.name}（${e.empId}） · 值班长` +
        (e.ddUserId ? ` · 钉钉:${e.ddUserId}` : " · 无钉钉号"),
      value: e.empId,
    }));

  const confirmStates = cm?.kind === "hr" ? opt?.hrConfirmStates : opt?.chiefConfirmStates;

  const EvidenceBlocks = ({ ev, status }: { ev: Evidence | null; status?: string }) => {
    if (ev == null) return <Spin />;
    const displayStatus = status ?? ev.status;
    return (
      <>
        <div style={{ color: "#888", marginBottom: 6 }}>
          自然日：<strong>{ev.date}</strong> ｜ 地区/工作点：{ev.region} · {ev.furnace} ｜ 对账：<Tag color={STATUS_COLOR[displayStatus]}>{displayStatus}</Tag>
          ｜ 出勤工时：<strong>{fmtHours(ev.attendanceResultWorkHours)}</strong>
        </div>
        <strong>工作点扫码</strong>
        {ev.scanRecords.length ? (
          <List size="small" dataSource={ev.scanRecords} renderItem={(s) => (
            <List.Item>
              <span><Tag>scan#{s.id}</Tag>{srcDate(s.naturalDate, ev.date)}{fmtTime(s.time)} · {s.region}{s.furnace} · {s.squad ?? "不分班"} · {s.post} · {s.employType}{isHalfShift(s.workHours) ? ` · ${workHoursLabel(s.workHours)}` : ""} · 来源{s.source}</span>
            </List.Item>
          )} />
        ) : " （无）"}
        <strong>补录 / 代签单</strong>
        {ev.makeups.length ? (
          <List size="small" dataSource={ev.makeups} renderItem={(m) => (
            <List.Item>
              <span><Tag>makeup#{m.id}</Tag>{srcDate(m.naturalDate, ev.date)}{m.kind} · {m.status} · {m.region}{m.furnace} · {m.squad ?? "不分班"} · {m.post}{m.approver ? ` · 经办${m.approver}` : ""}{m.reason ? ` · ${m.reason}` : ""}</span>
            </List.Item>
          )} />
        ) : " （无）"}
        <strong>加班</strong>
        {(ev.overtimes ?? []).length ? (
          <List size="small" dataSource={ev.overtimes ?? []} renderItem={(o) => (
            <List.Item>
              <span>
                <Tag color="volcano">OT#{o.id}</Tag>
                {srcDate(o.naturalDate, ev.date)}{o.region}{o.furnace} · {o.squad ?? "不分班"} · {o.post} · {o.employType} · {o.workHours}h
                {o.approver ? ` · 经办${o.approver}` : ""}
                <br />
                <Tag color={STATUS_COLOR[o.status]}>{o.status}</Tag>
                <Tag color={o.valid ? "green" : "red"}>{o.valid ? "有效" : "无效"}</Tag>
                {o.validReason}
              </span>
            </List.Item>
          )} />
        ) : " （无）"}
        <strong>钉钉打卡</strong>
        {ev.laoqinPunches.length ? (
          <List size="small" dataSource={ev.laoqinPunches} renderItem={(p) => (
            <List.Item>
              <span><Tag>punch#{p.id}</Tag>{srcDate(p.naturalDate, ev.date)}{p.time.slice(0, 19).replace("T", " ")} · {p.inoutType}{p.source ? ` · ${p.source}` : ""}{p.makeupReason ? <Tag color="orange" style={{ marginLeft: 4 }}>{p.makeupReason}</Tag> : null}</span>
            </List.Item>
          )} />
        ) : " （无）"}
      </>
    );
  };

  const confirmCol = (kind: "hr" | "chief", editable: boolean) => (_: unknown, r: Row) => {
    const cur = kind === "hr" ? r.hrStatus : r.chiefStatusDisplay;
    const chiefBlocked = kind === "chief" && r.chiefCanConfirm === false;
    const blockedHint = r.delegateFurnaceId
      ? r.rowKind === "overtime"
        ? `该员工当日审核已委托给 ${r.delegateRegion || "目标地区"}·${r.delegateFurnace || "目标工作点"} 值班长，加班行随整日一起确认`
        : `该行已委托给 ${r.delegateRegion || "目标地区"}·${r.delegateFurnace || "目标工作点"} 值班长`
      : "该行没有可确认的基础出勤工作点";
    return (
      <Space>
        <Tag color={CONFIRM_COLOR[cur] || "default"} data-testid={`${kind}-state-${r.empId}`}>{cur}</Tag>
        {editable && (
          <Button
            size="small"
            disabled={chiefBlocked}
            title={chiefBlocked ? blockedHint : undefined}
            data-testid={`${kind}-confirm-${r.empId}`}
            onClick={() => openConfirm(r, kind)}
          >
            确认
          </Button>
        )}
        {chiefBlocked && (
          <Tooltip title={blockedHint}>
            <span style={{ fontSize: 12, color: "#8c8c8c", cursor: "help" }}>受限</span>
          </Tooltip>
        )}
      </Space>
    );
  };

  // 列定义（带稳定 key 供「列显示/列宽」记忆），经 tc.decorate 应用本地偏好
  const anomalyColumns = [
    { key: "date", title: "日期", dataIndex: "date", width: 100 },
    {
      key: "region",
      title: "地区",
      width: 120,
      render: (_: unknown, r: Row) => {
        const regions = [...new Set((r.furnaces ?? []).map((item) => item.region).filter(Boolean))];
        if (r.regionPending) return <Tag color="gold">待归属</Tag>;
        return regions.length
          ? regions.map((region) => <Tag key={region}>{region}</Tag>)
          : "—";
      },
    },
    { key: "furnace", title: "出勤工作点", dataIndex: "furnace", width: 150,
      render: (f: string, r: Row) => {
        const list = r.furnaces && r.furnaces.length ? r.furnaces : (f ? [{ region: "", furnace: f }] : []);
        if (!list.length) return "—";
        return (
          <span data-testid={`furnaces-${r.empId}`}>
            {list.map((x, i) => (
              <span key={i} style={{ display: "block", whiteSpace: "nowrap" }}>
                {[x.region, x.furnace].filter(Boolean).join("·")}
              </span>
            ))}
          </span>
        );
      } },
    { key: "name", title: "姓名", dataIndex: "name", width: 120, render: (n: string, r: Row) => n || r.empId },
    { key: "chiefName", title: "出勤工作点的值班长", dataIndex: "chiefName", width: 140,
      render: (c: string, r: Row) => {
        const list = r.furnaceChiefs && r.furnaceChiefs.length
          ? r.furnaceChiefs.map((x) => x.name)
          : (c ? [c] : []);
        if (!list.length) return "—";
        return (
          <span data-testid={`chiefs-${r.empId}`}>
            {list.map((n, i) => (
              <span key={i} style={{ display: "block", whiteSpace: "nowrap" }}>{n}</span>
            ))}
          </span>
        );
      } },
    { key: "dataSource", title: "数据来源", dataIndex: "dataSource", width: 90, render: (s: string) => s || "—" },
    { key: "formalHours", title: "正式工时", dataIndex: "formalHours", width: 90,
      render: (h: number | undefined, r: Row) =>
        <span data-testid={`formalhours-${r.empId}`}>{h ? `${h}小时` : "—"}</span> },
    { key: "internHours", title: "实习工时", dataIndex: "internHours", width: 90,
      render: (h: number | undefined) => (h ? `${h}小时` : "—") },
    { key: "standbyHours", title: "待岗工时", dataIndex: "standbyHours", width: 90,
      render: (h: number | undefined) => (h ? `${h}小时` : "—") },
    { key: "status", title: "对账状态", dataIndex: "status", width: 100,
      render: (s: string) => <Tag data-testid={`anomaly-${s}`} color={STATUS_COLOR[s] || "default"}>{s}</Tag> },
    { key: "attendanceResultWorkHours", title: "出勤工时", dataIndex: "attendanceResultWorkHours", width: 90,
      render: (h: string | null | undefined) => fmtHours(h) },
    { key: "hr", title: "部门确认", width: 140, render: confirmCol("hr", hrEditable) },
    { key: "chief", title: "值班长确认", width: 150, render: confirmCol("chief", chiefEditable) },
    { key: "valid", title: "是否可用", width: 95,
      render: (_: unknown, r: Row) => <Tag color={r.valid ? "green" : "red"} data-testid={`valid-${r.empId}`}>{r.valid ? "班次有效" : "班次无效"}</Tag> },
    { key: "validReason", title: "确认理由", dataIndex: "validReason", width: 100, render: (s: string) => <span data-testid="valid-reason">{s}</span> },
    { key: "ops", title: "操作", width: 230, render: (_: unknown, r: Row) => {
      const k = rowKeyOf(r);
      const open = expandedKeys.includes(k);
      return (
        <Space size={0}>
          <Button type="link" size="small" onClick={() => setDrawer(r)}>证据链</Button>
          {r.rowKind === "overtime" && (
            <Button type="link" size="small" data-testid={`toggle-compare-${r.empId}`}
              onClick={() => setExpandedKeys((ks) => open ? ks.filter((x) => x !== k) : [...ks, k])}>
              {open ? "收起对照" : "考勤对照"}
            </Button>
          )}
          <Button type="link" size="small" data-testid={`resync-${r.empId}`}
            loading={resyncing === k} disabled={bulkRunning || (!!resyncing && resyncing !== k)}
            title="同步该员工当天及前后各一天的劳勤打卡、当天出勤工时并重新对账（仅内网可用）"
            onClick={() => resyncOne(r)}>
            重拉劳勤
          </Button>
        </Space>
      );
    } },
  ];
  const dec = tc.decorate<Row>(anomalyColumns as never);

  // 头部(标题+筛选)固定，表格内部滚动且列名固定：按头部实际高度动态算表体可用高度，
  // 这样筛选行换行/窗口缩放都能自适应，留出分页条空间。
  const headerRef = useRef<HTMLDivElement>(null);
  const [tableY, setTableY] = useState(420);
  useEffect(() => {
    const compute = () => {
      const top = headerRef.current?.getBoundingClientRect().bottom ?? 160;
      setTableY(Math.max(220, Math.round(window.innerHeight - top - 76)));
    };
    compute();
    window.addEventListener("resize", compute);
    const ro = new ResizeObserver(compute);
    if (headerRef.current) ro.observe(headerRef.current);
    return () => {
      window.removeEventListener("resize", compute);
      ro.disconnect();
    };
  }, []);

  return (
    <div style={{ height: "calc(100vh - 56px)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div ref={headerRef} style={{ flexShrink: 0 }}>
      <Space style={{ marginBottom: 12 }} align="center">
        <h2 data-testid="anomalies-title" style={{ margin: 0 }}>劳勤工作台</h2>
        {/* 待处理 = 自动无效（需 HR/值班长 处理）。按钮切换「仅待处理 / 全部显示」，状态记忆到本地 */}
        <Button
          danger={pendingOnly}
          type={pendingOnly ? "primary" : "default"}
          data-testid="pending-filter"
          disabled={loading}
          onClick={togglePending}
        >
          {pendingOnly ? `仅看待处理 ${data?.pendingCount ?? 0}` : `全部显示 · 待处理 ${data?.pendingCount ?? 0}`}
        </Button>
        {/* 一键展开/收起当前页全部行的考勤对照（偏好记忆到本地） */}
        <Button data-testid="toggle-expand-all" onClick={toggleExpandAll}>
          {expandAll ? "收起全部考勤对照" : "展开全部考勤对照"}
        </Button>
        {["admin", "hr", "stat"].includes(role) && <>
          <SyncLogButton group="attendance" active={active} />
          <Button data-testid="bulk-resync" onClick={startBulk}
            loading={bulkStarting || bulkRunning}
            disabled={!fromStr || !toStr || !!resyncing || bulkRunning}>
            批量重拉异常
          </Button>
          {bulk.id && <Button type="link" onClick={() => setBulkDetails(true)}>
            补拉进度 {bulk.done ?? 0}/{bulk.total ?? 0} · 恢复 {bulk.counts?.recovered ?? 0}
          </Button>}
        </>}
      </Space>
      <Modal title="批量重拉结果" open={bulkDetails} onCancel={() => setBulkDetails(false)}
        footer={<Button onClick={() => setBulkDetails(false)}>关闭</Button>} width={800}>
        <p>{bulk.dateFrom} 至 {bulk.dateTo} · {bulk.region || "全部地区"} · {
          bulk.status === "done" ? "已完成" : bulk.status === "failed" ? "准备失败，请重新发起" : bulkRunning ? "执行中" : "等待执行或续跑"}</p>
        <p>已处理 {bulk.done ?? 0}/{bulk.total ?? 0}，恢复正常 {bulk.counts?.recovered ?? 0}，
          仍异常 {bulk.counts?.unchanged ?? 0}，跳过 {bulk.counts?.skipped ?? 0}，失败 {bulk.counts?.failed ?? 0}</p>
        {bulk.error && <p style={{ color: "#cf1322" }}>{bulk.error}</p>}
        <p>最近 100 条记录；仍异常表示已取得最新数据，但尚未满足考勤规则。</p>
        <Table size="small" rowKey={(r) => `${r.empId}:${r.date}`} dataSource={bulk.items ?? []}
          pagination={{ pageSize: 10 }} columns={[
            { title: "工号", dataIndex: "empId" }, { title: "日期", dataIndex: "date" },
            { title: "结果", dataIndex: "status", render: (v: string) => ({ recovered: "恢复正常", unchanged: "仍异常", skipped: "跳过", failed: "失败", pending: "待执行", running: "执行中" }[v] || v) },
            { title: "工时（前 → 后）", render: (_, r) => `${r.before?.rows?.[0]?.attendanceResultWorkHours ?? "—"} → ${r.after?.rows?.[0]?.attendanceResultWorkHours ?? "—"}` },
            { title: "说明", dataIndex: "error" },
          ]} />
      </Modal>
      <Space wrap style={{ marginBottom: 12, display: "flex" }}>
        <DatePicker
          picker="month"
          allowClear
          value={quickMonth}
          placeholder="快速选择月份"
          disabled={loading}
          onChange={(v) => applyQuickMonth(v)}
          data-testid="anomaly-month"
        />
        <Button disabled={loading} onClick={() => applyQuickMonth(dayjs())}>本月</Button>
        <RangePicker
          allowClear
          value={range}
          placeholder={["开始日期", "结束日期"]}
          disabled={loading}
          onChange={(v) => {
            if (loadingRef.current) return;
            setRange(v);
            setQuickMonth(v?.[0] ?? null);
          }}
          data-testid="anomaly-date"
        />
        <Select
          allowClear={!getPageRegionScope()}
          placeholder="地区"
          style={{ width: 140 }}
          value={getPageRegionScope() || filters.region}
          disabled={loading || !!getPageRegionScope()}
          onChange={(value) => setFilters((current) => ({ ...current, region: value }))}
          options={(getPageRegionScope() ? [getPageRegionScope()] : opt?.regions ?? [])
            .filter((value): value is string => !!value)
            .map((value) => ({ label: value, value }))}
        />
        {sel("出勤工作点", "furnace", opt?.furnaces?.length ? opt.furnaces : fallbackFurnaceOptions)}
        <Input allowClear placeholder="姓名" style={{ width: 110 }} value={nameInput}
          disabled={loading}
          onChange={(e) => setNameInput(e.target.value)}
          onPressEnter={refreshCurrentSearch} />
        <Input allowClear placeholder="出勤工作点的值班长名字" style={{ width: 150 }} value={chiefInput}
          disabled={loading}
          onChange={(e) => setChiefInput(e.target.value)}
          onPressEnter={refreshCurrentSearch} />
        {sel("是否可用", "validKind", opt?.validKinds)}
        {sel("对账状态", "status", opt?.statuses)}
        {sel("数据来源", "dataSource", opt?.dataSources)}
        {sel("部门确认", "hrStatus", opt?.confirmStates)}
        {sel("值班长确认", "chiefStatus", opt?.confirmStates)}
        {sel("有无扫码", "scanData", opt?.scanData)}
        {sel("钉钉数据", "laoqin", opt?.laoqinBuckets)}
        <Button type="primary" loading={loading} disabled={loading} onClick={refreshCurrentSearch}>搜索</Button>
        <Button
          disabled={loading}
          onClick={() => {
            setNameInput("");
            setChiefInput("");
            setFilters(pendingOnly ? { validKind: PENDING_VALID_KIND } : {});
            setPage(1);
            setExpandedKeys([]);
            setRefreshSeq((v) => v + 1);
          }}
        >
          重置
        </Button>
        <tc.Toggles />
      </Space>
      {loadError && (
        <Alert
          data-testid="anomaly-load-error"
          type={loadError.status === 429 ? "info" : loadError.status === 422 ? "warning" : "error"}
          showIcon
          style={{ marginBottom: 12 }}
          message={loadError.status === 422
            ? "本次报表数据量过大"
            : loadError.status === 429
              ? "请求过于频繁"
              : "劳勤工作台加载失败"}
          description={loadError.status === 422
            ? "请选择出勤工作点或缩小日期范围后重试。"
            : loadError.status === 429
              ? "请稍后再试，避免连续切换筛选条件。"
              : loadError.detail}
          action={<Button size="small" onClick={refreshCurrentSearch}>重试</Button>}
        />
      )}
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
      <Table
        rowKey={rowKeyOf}
        loading={loading}
        dataSource={items}
        pagination={{
          current: page,
          pageSize,
          total: data?.count ?? 0,
          showSizeChanger: false,
          showTotal: (t) => `共 ${t} 条`,
          disabled: loading,
          onChange: (p) => {
            if (loadingRef.current) return;
            setPage(p);
            setExpandedKeys([]);
          },
        }}
        data-testid="anomaly-list"
        expandable={{
          // 展开行：在该行数据下方内嵌「考勤对照」时间轴图，方便 HR 直接核对
          // 不显示默认展开列，改由「操作」列里的按钮控制展开/收起
          expandedRowRender: (r) => <RowTimeline empId={r.empId} date={r.date} overtimeId={r.overtimeId} />,
          showExpandColumn: false,
          expandedRowKeys: expandedKeys,
          onExpandedRowsChange: (keys) => setExpandedKeys(keys.map(String)),
        }}
        columns={dec.columns}
        components={tc.components}
        scroll={{ x: dec.scrollX, y: tableY }}
      />
      </div>

      {/* 证据链抽屉 */}
      <Drawer open={!!drawer} onClose={() => setDrawer(null)} title="证据链" width={520} data-testid="anomaly-evidence-drawer">
        {drawer && (
          <>
            <p style={{ lineHeight: 2 }}>
              <strong>{drawer.name ?? drawer.empId}</strong> · {drawer.empId}<br />
              数据来源：{drawer.dataSource || "—"} ｜ 是否可用：
              <Tag color={drawer.valid ? "green" : "red"}>{drawer.valid ? "班次有效" : "班次无效"}</Tag>
              理由：{drawer.validReason}
            </p>
            <EvidenceBlocks ev={evidence} status={drawer.status} />
          </>
        )}
      </Drawer>

      {/* 确认弹窗 */}
      <Modal
        open={!!cm}
        title={cm ? `${cm.kind === "hr" ? "部门确认" : "值班长确认"}：${cm.row.name ?? cm.row.empId} · ${cm.row.furnace}` : ""}
        onCancel={() => setCm(null)}
        confirmLoading={submitting}
        okText="提交确认"
        onOk={() => void doConfirm(undefined, pickedChief)}
        okButtonProps={{
          disabled:
            !pick
            || (
              cm?.kind === "hr"
              && pick === "待值班长确认"
              && (!cm.row.delegateChiefName || cm.needChiefOverride)
              && !pickedChief
            ),
        }}
        width={560}
        data-testid="confirm-modal"
      >
        {cm && (
          <>
            <div style={{ margin: "0 0 8px", padding: "8px 10px", background: "#f7f9fc", borderRadius: 6, lineHeight: 1.9 }}>
              <div>工作点：<strong>{cm.row.furnace}</strong></div>
              <div>
                当前行工作点值班长：
                {cm.row.chiefName ? (
                  <>
                    <strong>{cm.row.chiefName}</strong>（工号 {cm.row.chiefEmpId || "—"}）· 钉钉：
                    {cm.row.chiefDdUserId ? cm.row.chiefDdUserId : <Tag color="orange">无钉钉号</Tag>}
                  </>
                ) : (
                  <Tag color="orange">未绑定</Tag>
                )}
              </div>
              <div>
                委托目标：
                {cm.row.delegateFurnaceId ? (
                  <>
                    <strong>{cm.row.delegateRegion}·{cm.row.delegateFurnace}</strong>；值班长：
                    {cm.row.delegateChiefName ? (
                      <>
                        <strong>{cm.row.delegateChiefName}</strong>
                        （工号 {cm.row.delegateChiefEmpId || "—"}）· 钉钉：
                        {cm.row.delegateChiefDdUserId
                          ? cm.row.delegateChiefDdUserId
                          : <Tag color="orange">无钉钉号</Tag>}
                      </>
                    ) : (
                      <Tag color="orange">未绑定在职值班长</Tag>
                    )}
                  </>
                ) : cm.row.delegateOutOfScope ? (
                  <Tag color="orange">不在当前管辖地区</Tag>
                ) : (
                  <Tag color="orange">无法定位</Tag>
                )}
              </div>
              <div>
                常用值班长（操作工绑定）：
                {cm.row.boundChief ? (
                  <>
                    <strong>{cm.row.boundChief.name}</strong>（工号 {cm.row.boundChief.empId}）· 钉钉：
                    {cm.row.boundChief.ddUserId ? cm.row.boundChief.ddUserId : <Tag color="orange">无钉钉号</Tag>}
                  </>
                ) : (
                  <Tag color="orange">未绑定</Tag>
                )}
              </div>
            </div>
            <p style={{ margin: "0 0 8px" }}>
              当前：对账 <Tag color={STATUS_COLOR[cm.row.status]}>{cm.row.status}</Tag>
              是否可用 <Tag color={cm.row.valid ? "green" : "red"}>{cm.row.valid ? "班次有效" : "班次无效"}</Tag>
              （理由：{cm.row.validReason}）
            </p>
            <div style={{ maxHeight: 220, overflow: "auto", border: "1px solid #f0f0f0", borderRadius: 6, padding: 10, marginBottom: 12 }}>
              <EvidenceBlocks ev={cmEvidence} status={cm.row.status} />
            </div>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>设置状态</div>
            <Radio.Group value={pick} onChange={(e) => setPick(e.target.value)} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {(confirmStates ?? []).map((s) => (
                <Radio
                  key={s}
                  value={s}
                  disabled={
                    s === "待值班长确认"
                    && (
                      !!cm.row.delegateOutOfScope
                      || !cm.row.delegateFurnaceId
                      || !cm.row.delegateRegion
                    )
                  }
                  data-testid={`pick-${s}`}
                  style={{ alignItems: "flex-start" }}
                >
                  <span style={{ display: "block" }}>
                    <Tag color={CONFIRM_COLOR[s] || "default"} style={{ marginInlineEnd: 0 }}>{s}</Tag>
                    <span style={{ display: "block", color: "#888", fontSize: 12, lineHeight: 1.5, marginTop: 3 }}>
                      {explainState(s, cm.row)}
                    </span>
                  </span>
                </Radio>
              ))}
            </Radio.Group>
            {/* HR 选「待值班长确认」且该工作点无值班长 → 指定值班长 */}
            {cm.kind === "hr" && pick === "待值班长确认" && (!cm.row.delegateChiefName || cm.needChiefOverride) && (
              <div style={{ marginTop: 12, padding: 10, background: "#fffbe6", borderRadius: 6 }}>
                <div style={{ marginBottom: 6, color: "#ad6800" }}>
                  {cm.needChiefHint || "该工作点无在职值班长，请指定同地区的在职值班长（提交后绑定并推送）："}
                </div>
                <Space direction="vertical" size={4}>
                  <Select showSearch placeholder="选择同地区在职值班长" style={{ width: 320 }} value={pickedChief}
                    optionFilterProp="label" options={chiefOptions}
                    onChange={(v) => setPickedChief(v)} data-testid="chief-picker" />
                  {chiefCandidatesState === "loading" && (
                    <span style={{ fontSize: 12, color: "#8c8c8c" }}><Spin size="small" /> 正在加载候选值班长…</span>
                  )}
                  {chiefCandidatesState === "error" && (
                    <Space size={6}>
                      <span style={{ fontSize: 12, color: "#cf1322" }}>候选值班长加载失败，请重试。</span>
                      <Button size="small" onClick={loadChiefCandidates}>重新加载</Button>
                    </Space>
                  )}
                  {chiefCandidatesState === "incompatible" && (
                    <span style={{ fontSize: 12, color: "#cf1322" }}>
                      服务端版本不匹配，请等待后端部署完成后刷新页面。
                    </span>
                  )}
                  {chiefCandidatesState === "ready" && !chiefOptions.length && (
                    <span style={{ fontSize: 12, color: "#8c8c8c" }}>
                      暂无符合条件的候选人，请先在“员工管理”设置值班长角色和所属地区。
                    </span>
                  )}
                </Space>
              </div>
            )}
          </>
        )}
      </Modal>
    </div>
  );
}
