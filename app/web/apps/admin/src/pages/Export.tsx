import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ExclamationCircleOutlined, LockFilled, UnlockOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Checkbox, DatePicker, Input, InputNumber, Popconfirm, Select, Space, Statistic, Table, Tag, Tooltip, message } from "antd";
import dayjs from "dayjs";
import { ReportRequestQueue, isReportCancelled, type QueueStatus } from "../reportRequestQueue";
import { getPageCacheEpoch, usePageCache } from "../pageCache";
import {
  getReportRequestStatus,
  type ReportRequestStatus,
  getAbsenceOverrides,
  saveAbsenceOverride,
  type AbsenceOverride,
  downloadExport,
  downloadExportSheet,
  getDemoContext,
  getExportButtonVisibility,
  getFurnaces,
  getMe,
  getMonthlyExportLock,
  getMonthlyExportLocks,
  previewExportSheet,
  previewMonthlyHours,
  previewOvertimeDetail,
  setMonthlyExportLock,
  setExportButtonVisibility,
  type ExportButtonKey,
  type ExportButtonVisibility,
  type ExportVisibleRole,
  type ExportMeta,
  type ExtraSheet,
  type FurnaceItem,
  type MonthlyHoursMeta,
  type MonthlyExportLockStatus,
  type MonthlyExportLockSummary,
  type OvertimeDetailMeta,
  type PayrollRow,
} from "@fdc/api-client";

// 薪资预览表的关键列（对应「输出给薪资」中可计算/派生字段，非全 45+ 列）
const PAYROLL_PREVIEW_BASE_COLS = [
  "工号",
  "姓名",
  "部门",
  "岗位",
  "用工类型",
  "薪资比例系数",
  "应出勤天数",
  "实际出勤天数",
  "旷工天数",
  "加班合计",
] as const;

// 用工类型 → 展示色
const EMPLOY_TYPE_COLOR: Record<string, string> = {
  正式: "green",
  实习: "blue",
  待岗: "orange",
};

// 封账是辅助状态，使用低饱和暖灰红，避免与错误/危险操作的高强度红色混淆。
const SOFT_LOCK_TAG_STYLE = {
  color: "#956f6b",
  background: "#fcf7f6",
  borderColor: "#eadbd8",
};
const SOFT_LOCK_ICON_STYLE = { color: "#9b7773", opacity: 0.72, fontSize: 12 };
const UNLOCKED_ICON_STYLE = { color: "#6f927d", opacity: 0.78, fontSize: 12 };

function ratioText(r: string | number): string {
  const n = Number(r);
  if (!Number.isFinite(n)) return r ? String(r) : "-";
  return `${Math.round(n * 100)}%`;
}

function toObjects(sheet: ExtraSheet): Record<string, string | number>[] {
  return sheet.rows.map((r) =>
    Object.fromEntries(sheet.columns.map((c, i) => [c, r[i] ?? ""])),
  );
}

// ---- 预览表「工号/姓名」筛选：纯前端展示层过滤，不影响导出（导出函数读取的是未过滤的原始行）----
function filterObjectRows<T extends Record<string, unknown>>(rows: T[], keyword: string): T[] {
  const q = keyword.trim();
  if (!q) return rows;
  return rows.filter(
    (r) => String(r["工号"] ?? "").includes(q) || String(r["姓名"] ?? "").includes(q),
  );
}

// 输出给薪资会按班组/岗位拆行，同一工号可能出现多次；必须保证 React 行 key 唯一，
// 否则筛选后 Ant Table 会因重复 key 残留其他员工的旧行。
function payrollRowKey(row: PayrollRow, index?: number): string {
  return [
    row["工号"] ?? "",
    row["部门"] ?? "",
    row["岗位"] ?? "",
    row["用工类型"] ?? "",
    index ?? "",
  ].join("|");
}

// 数组行（GridRow）按列名定位「工号」列及所有含「姓名」二字的列（如「加班员工姓名」），无匹配列时筛选不生效
function filterGridRows(
  rows: GridRow[] | undefined,
  cols: string[] | undefined,
  keyword: string,
): GridRow[] {
  const all = rows ?? [];
  const q = keyword.trim();
  if (!q || !cols) return all;
  const idIdx = cols.indexOf("工号");
  const nameIndexes = cols.flatMap((col, index) => (col.includes("姓名") ? [index] : []));
  if (idIdx < 0 && nameIndexes.length === 0) return all;
  return all.filter((row) => {
    const id = idIdx >= 0 ? String(row[idIdx] ?? "") : "";
    return id.includes(q) || nameIndexes.some((index) => String(row[index] ?? "").includes(q));
  });
}

// 各预览表独立维护「草稿输入」与「已生效关键字」：仅点击筛选按钮（或回车）才把草稿提交为生效值，
// 打字过程不触发筛选，避免误以为实时过滤。
function useKeywordFilters<K extends string>(keys: readonly K[]) {
  const empty = () => Object.fromEntries(keys.map((k) => [k, ""])) as Record<K, string>;
  const [drafts, setDrafts] = useState<Record<K, string>>(empty);
  const [applied, setApplied] = useState<Record<K, string>>(empty);
  const setDraft = (k: K, v: string) => setDrafts((d) => ({ ...d, [k]: v }));
  const apply = (k: K) => setApplied((a) => ({ ...a, [k]: (drafts[k] ?? "").trim() }));
  const clear = (k: K) => {
    setDrafts((d) => ({ ...d, [k]: "" }));
    setApplied((a) => ({ ...a, [k]: "" }));
  };
  return { drafts, applied, setDraft, apply, clear };
}

// 旷工没有数据与数值为零统一显示 0，其它列继续使用原有空值占位。
function absenceValue(v: string | number | null | undefined): string | number {
  return v == null || (typeof v === "string" && ["", "-", "0"].includes(v.trim())) ? 0 : v;
}

// 数组行单元格展示：空→占位；去掉 Excel 文本前缀 '（导出文件里用于保留前导零，Excel 中不显示）
function cellText(v: string | number | undefined) {
  if (v === "" || v == null) return <span style={{ color: "#bbb" }}>-</span>;
  const s = String(v);
  return s.startsWith("'") ? s.slice(1) : s;
}

function comparePreviewValues(
  left: string | number | undefined,
  right: string | number | undefined,
) {
  const normalize = (value: string | number | undefined) =>
    String(value ?? "").replace(/^'/, "").trim();
  const a = normalize(left);
  const b = normalize(right);
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  const aNumber = Number(a);
  const bNumber = Number(b);
  if (Number.isFinite(aNumber) && Number.isFinite(bNumber)) {
    return aNumber - bNumber;
  }
  return a.localeCompare(b, "zh-CN", { numeric: true, sensitivity: "base" });
}

// 通用「数组行」表格列（班次有效工时 / 加班明细的 meta 行是与 Excel 一致的数组）
type GridRow = (string | number)[];
const buildGridColumns = (cols: string[], widthOf?: (col: string, i: number) => number) =>
  cols.map((c, i) => ({
    title: c,
    key: `${c}-${i}`,
    width: widthOf ? widthOf(c, i) : 110,
    ellipsis: true,
    sorter: (a: GridRow, b: GridRow) => comparePreviewValues(a[i], b[i]),
    render: (_: unknown, row: GridRow) => cellText(c === "旷工天数" ? absenceValue(row[i]) : row[i]),
  }));

// 记住上次工作点选择（仅保留仍存在的 id，去掉已停用的工作点）
const FURNACE_STORAGE_KEY = "fdc-export-furnaces";
const MONTH_STORAGE_KEY = "fdc-export-month";
const PAGE_SIZE_STORAGE_KEY = "fdc-export-preview-page-sizes";
const PAYROLL2_GROUP_STORAGE_KEY = "fdc-export-payroll2-group-by";
const PAYROLL2_HIDE_ZERO_ROWS_STORAGE_KEY = "fdc-export-payroll2-hide-zero-rows";
const PAYROLL2_MERGE_OVERTIME_ONLY_ROWS_STORAGE_KEY =
  "fdc-export-payroll2-merge-overtime-only-rows";
const PAYROLL2_MERGE_SAME_SQUAD_OVERTIME_ONLY_ROWS_STORAGE_KEY =
  "fdc-export-payroll2-merge-same-squad-overtime-only-rows";
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
const PAYROLL2_GROUP_FIELDS = ["工号", "工作点", "班组", "岗位", "用工类型"] as const;
const DEFAULT_PAYROLL2_GROUP_BY = ["工号", "工作点", "班组", "用工类型"];
const PAYROLL2_MERGE_TOOLTIP =
  "按原规则为零出勤加班行寻找该员工的有效出勤行；开关同时影响预览和导出。";
const PAYROLL2_MERGE_SAME_SQUAD_TOOLTIP =
  "仅将同一工号、同工作点、同班组、同用工类型的零出勤加班行并入该班组已有出勤行，岗位可以不同；该班组没有出勤时保留原加班行。";
const EXPORT_BUTTON_KEYS: ExportButtonKey[] = [
  "combined",
  "payroll",
  "payroll2",
  "anomaly",
  "tripartite",
  "hours",
  "overtime",
  "overtimeSummary",
  "overtimePerson",
];
const DEFAULT_EXPORT_BUTTON_VISIBILITY = Object.fromEntries(
  EXPORT_BUTTON_KEYS.map((key) => [key, ["hr", "stat"]]),
) as ExportButtonVisibility;

type SectionKey =
  | "payroll"
  | "payroll2"
  | "anomaly"
  | "tripartite"
  | "hours"
  | "overtime"
  | "overtimeSummary"
  | "overtimePerson";

const SECTION_LABEL: Record<SectionKey, string> = {
  payroll: "输出给薪资",
  payroll2: "输出给薪资2",
  anomaly: "打卡异常",
  tripartite: "三方对账（劳勤/工作点/补录）",
  hours: "班次有效工时",
  overtime: "加班明细",
  overtimeSummary: "加班汇总",
  overtimePerson: "按人汇总",
};

// 可按「工号/姓名」筛选的表：除「加班汇总」外（该表按工作点编号×岗位×班次分组，不含个人列）
type FilterableSectionKey = Exclude<SectionKey, "overtimeSummary">;
const FILTERABLE_KEYS: FilterableSectionKey[] = [
  "payroll",
  "payroll2",
  "anomaly",
  "tripartite",
  "hours",
  "overtime",
  "overtimePerson",
];

const DEFAULT_PAGE_SIZES: Record<SectionKey, number> = {
  payroll: 10,
  payroll2: 10,
  anomaly: 10,
  tripartite: 10,
  hours: 10,
  overtime: 10,
  overtimeSummary: 10,
  overtimePerson: 10,
};

const DEFAULT_PAGES: Record<SectionKey, number> = {
  payroll: 1,
  payroll2: 1,
  anomaly: 1,
  tripartite: 1,
  hours: 1,
  overtime: 1,
  overtimeSummary: 1,
  overtimePerson: 1,
};

type PreviewSectionProps = {
  title: string;
  totalCount: number;
  visibleCount?: number;
  unit: string;
  filterActive?: boolean;
  actions: ReactNode;
  filterControls?: ReactNode;
  children: ReactNode;
};

// 必须保持为模块级组件：如果在 ExportPage 内每次 render 都重新定义，受控筛选输入框会因
// element.type 改变而被卸载/重挂载，造成每输入一个字符就丢失焦点。
function PreviewSection({
  title,
  totalCount,
  visibleCount = totalCount,
  unit,
  filterActive = false,
  actions,
  filterControls,
  children,
}: PreviewSectionProps) {
  return (
    <Card
      size="small"
      title={
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
          <Space wrap>
            <span>{title}</span>
            <Tag>{filterActive ? `${visibleCount} / ${totalCount} ${unit}` : `${totalCount} ${unit}`}</Tag>
          </Space>
          {actions}
        </div>
      }
    >
      {filterControls}
      {children}
    </Card>
  );
}

function ManualAbsenceEditor({ value, onSave }: {
  value: AbsenceOverride | undefined;
  onSave: (days: number | null, revision: number) => Promise<void>;
}) {
  const [draft, setDraft] = useState<number | null>(value?.days ?? null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { setDraft(value?.days ?? null); }, [value?.days, value?.revision]);
  if (!value) return <span style={{ color: "#999" }}>待加载</span>;
  const save = async () => {
    setSaving(true);
    try { await onSave(draft, value.revision); } finally { setSaving(false); }
  };
  return <Tooltip title={value.reason || `最多可填 ${value.maxDays} 天（截至昨天扣除有效出勤和请假）；按员工＋月份共享；导出优先采用手动值，清空则使用自动值`}>
    <Space size={4}>
      <InputNumber aria-label="手动旷工天数" min={0} max={value.maxDays} precision={2} step={0.01}
        placeholder="自动" value={draft} onChange={setDraft}
        disabled={!value.editable || saving} style={{ width: 88 }} />
      <Button size="small" loading={saving} disabled={!value.editable || draft === value.days}
        onClick={() => void save()}>保存</Button>
    </Space>
  </Tooltip>;
}

export default function ExportPage({ active = true }: { active?: boolean }) {
  const { invalidate } = usePageCache();
  const [reportQueue] = useState(() => new ReportRequestQueue());
  const [queueStatus, setQueueStatus] = useState<QueueStatus>({ pending: 0, label: "", waiting: false });
  const [reportStatus, setReportStatus] = useState<ReportRequestStatus | null>(null);
  const [reportStatusError, setReportStatusError] = useState("");
  const [fullRefreshing, setFullRefreshing] = useState(false);
  const [fullProgress, setFullProgress] = useState(0);
  const fullRefreshRef = useRef(false);
  const activeRef = useRef(active);
  activeRef.current = active;
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  useEffect(() => reportQueue.subscribe(setQueueStatus), [reportQueue]);
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const status = await getReportRequestStatus();
        if (!stopped) { setReportStatus(status); setReportStatusError(""); }
      } catch {
        if (!stopped) setReportStatusError("占用状态暂时无法更新，以下信息可能已过时");
      } finally {
        if (!stopped) timer = setTimeout(poll, 3000);
      }
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [active]);
  const previousActive = useRef(active);
  const [absenceOverrides, setAbsenceOverrides] = useState<Record<string, AbsenceOverride>>({});
  const [absenceLoadError, setAbsenceLoadError] = useState("");
  const absenceRequestRef = useRef(0);
  const lastAbsenceRead = useRef<{ key: string; snapshot: string } | null>(null);
  const manualSaveRef = useRef(false);
  const [manualSaving, setManualSaving] = useState(false);
  // 每张手动生成的表记录缓存纪元，发生数据写入后切回时按需刷新。
  const previewFreshnessRef = useRef<Partial<Record<SectionKey, {
    epoch: number; paramsKey: string;
  }>>>({});
  const [furnaces, setFurnaces] = useState<FurnaceItem[]>([]);
  const [month, setMonth] = useState("");
  const [anchorMonth, setAnchorMonth] = useState("");
  // 追踪当前选中月份：封账写请求飞行期间可能切月，响应回来时据此判断归属。
  const monthRef = useRef(month);
  monthRef.current = month;
  const [selected, setSelected] = useState<number[]>([]);

  const [payrollSheet, setPayrollSheet] = useState<ExportMeta["payrollSheet"] | null>(null);
  const [payrollSheet2, setPayrollSheet2] = useState<ExportMeta["payrollSheet"] | null>(null);
  const [anomalySheet, setAnomalySheet] = useState<ExtraSheet | null>(null);
  const [tripartiteSheet, setTripartiteSheet] = useState<ExtraSheet | null>(null);
  const [furnaceCount, setFurnaceCount] = useState(0);
  const [exportWarnings, setExportWarnings] = useState<ExportMeta["warnings"]>([]);
  const [monthlyHours, setMonthlyHours] = useState<MonthlyHoursMeta | null>(null);
  const [overtimeDetail, setOvertimeDetail] = useState<OvertimeDetailMeta | null>(null);
  const [sectionLoading, setSectionLoading] = useState<Record<SectionKey, boolean>>({
    payroll: false,
    payroll2: false,
    anomaly: false,
    tripartite: false,
    hours: false,
    overtime: false,
    overtimeSummary: false,
    overtimePerson: false,
  });
  const [downloading, setDownloading] = useState(false);
  const [sectionDownloading, setSectionDownloading] = useState<Record<SectionKey, boolean>>({
    payroll: false,
    payroll2: false,
    anomaly: false,
    tripartite: false,
    hours: false,
    overtime: false,
    overtimeSummary: false,
    overtimePerson: false,
  });
  const [role, setRole] = useState("");
  const [exportButtonVisibility, setExportButtonVisibilityState] =
    useState<ExportButtonVisibility>(DEFAULT_EXPORT_BUTTON_VISIBILITY);
  const [visibilityLoaded, setVisibilityLoaded] = useState(false);
  const [visibilitySaving, setVisibilitySaving] = useState(false);
  const [monthLocks, setMonthLocks] = useState<MonthlyExportLockStatus | null>(null);
  const [yearLocks, setYearLocks] = useState<MonthlyExportLockSummary[]>([]);
  const [lockLoadingKey, setLockLoadingKey] = useState<string | null>(null);
  const [pageSizes, setPageSizes] = useState<Record<SectionKey, number>>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(PAGE_SIZE_STORAGE_KEY) || "{}");
      return { ...DEFAULT_PAGE_SIZES, ...(raw && typeof raw === "object" ? raw : {}) };
    } catch {
      return DEFAULT_PAGE_SIZES;
    }
  });
  const [pages, setPages] = useState<Record<SectionKey, number>>(DEFAULT_PAGES);
  const [refreshInterrupted, setRefreshInterrupted] = useState(false);
  const [previewError, setPreviewError] = useState<{ status?: number; detail: string } | null>(null);
  const kw = useKeywordFilters(FILTERABLE_KEYS);
  const applyKeywordFilter = (key: FilterableSectionKey) => {
    kw.apply(key);
    setPages((prev) => ({ ...prev, [key]: 1 }));
  };
  const clearKeywordFilter = (key: FilterableSectionKey) => {
    kw.clear(key);
    setPages((prev) => ({ ...prev, [key]: 1 }));
  };
  const [payroll2GroupBy, setPayroll2GroupBy] = useState<string[]>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(PAYROLL2_GROUP_STORAGE_KEY) || "[]");
      const fields = Array.isArray(raw) ? raw.filter((x) => PAYROLL2_GROUP_FIELDS.includes(x)) : [];
      return fields.includes("工号") ? fields : DEFAULT_PAYROLL2_GROUP_BY;
    } catch {
      return DEFAULT_PAYROLL2_GROUP_BY;
    }
  });
  const [payroll2HideZeroRows, setPayroll2HideZeroRows] = useState(() => {
    try {
      return localStorage.getItem(PAYROLL2_HIDE_ZERO_ROWS_STORAGE_KEY) !== "0";
    } catch {
      return true;
    }
  });
  const [payroll2MergeOvertimeOnlyRows, setPayroll2MergeOvertimeOnlyRows] = useState(() => {
    try {
      return localStorage.getItem(PAYROLL2_MERGE_OVERTIME_ONLY_ROWS_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [payroll2MergeSameSquadOvertimeOnlyRows, setPayroll2MergeSameSquadOvertimeOnlyRows] = useState(() => {
    try {
      return localStorage.getItem(PAYROLL2_MERGE_SAME_SQUAD_OVERTIME_ONLY_ROWS_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    getMe().then((m) => setRole(m.role)).catch(() => setRole(""));
    getExportButtonVisibility()
      .then((r) => setExportButtonVisibilityState(r.buttons))
      .catch(() => setExportButtonVisibilityState(DEFAULT_EXPORT_BUTTON_VISIBILITY))
      .finally(() => setVisibilityLoaded(true));
    // 月份锚点以服务端时间为准（Asia/Shanghai），避免浏览器本地时区偏差；请求失败回退本地时区。
    const applyCurrentMonth = (currentMonth: string) => {
      setAnchorMonth(currentMonth);
      let savedMonth = "";
      try {
        savedMonth = localStorage.getItem(MONTH_STORAGE_KEY) || "";
      } catch {
        /* ignore */
      }
      setMonth(
        MONTH_RE.test(savedMonth) && savedMonth <= currentMonth
          ? savedMonth
          : currentMonth,
      );
    };
    getDemoContext()
      .then((ctx) => applyCurrentMonth(ctx.month))
      .catch(() => applyCurrentMonth(dayjs().format("YYYY-MM")));
    getFurnaces().then((r) => {
      setFurnaces(r.items);
      const allIds = r.items.map((f) => f.id);
      // 仅恢复之前主动选择的部分工作点；旧版默认全选的记录不再自动触发整月范围。
      let init: number[] = [];
      try {
        const saved = JSON.parse(localStorage.getItem(FURNACE_STORAGE_KEY) || "[]");
        if (Array.isArray(saved) && saved.length) {
          const valid = saved.filter((id: number) => allIds.includes(id));
          if (valid.length && valid.length < allIds.length) init = valid;
        }
      } catch {
        /* ignore */
      }
      setSelected(init);
    });
  }, []);

  const visibleMonths = useMemo(
    () => anchorMonth
      ? Array.from({ length: 7 }, (_v, i) =>
        dayjs(`${anchorMonth}-01`).add(i - 6, "month").format("YYYY-MM"))
      : [],
    [anchorMonth],
  );
  const visibleYearsKey = [...new Set(visibleMonths.map((item) => item.slice(0, 4)))].join(",");
  const yearLockByMonth = useMemo(
    () => new Map(yearLocks.map((item) => [item.month, item])),
    [yearLocks],
  );

  const applyMonth = (next: string) => {
    setMonth(next);
    try {
      localStorage.setItem(MONTH_STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
    resetPreviews();
  };

  useEffect(() => {
    const visibleYears = visibleYearsKey.split(",").filter(Boolean);
    if (!visibleYears.length) {
      setYearLocks([]);
      return;
    }
    let alive = true;
    Promise.all(visibleYears.map((year) => getMonthlyExportLocks(year)))
      .then((responses) => {
        if (alive) setYearLocks(responses.flatMap((res) => res.items));
      })
      .catch(() => {
        if (alive) setYearLocks([]);
      });
    return () => {
      alive = false;
    };
  }, [visibleYearsKey]);

  useEffect(() => {
    if (!month) {
      setMonthLocks(null);
      return;
    }
    let alive = true;
    getMonthlyExportLock(month)
      .then((s) => {
        if (alive) setMonthLocks(s);
      })
      .catch(() => {
        if (alive) setMonthLocks(null);
      });
    return () => {
      alive = false;
    };
  }, [month]);

  const monthLockByFurnace = useMemo(
    () => new Map((monthLocks?.items ?? []).map((item) => [item.furnaceId, item])),
    [monthLocks],
  );

  // 工作点直接按地区分块展示，选择方式与月份按钮一致。
  const furnaceRegionGroups = useMemo(() => {
    const byRegion = new Map<string, FurnaceItem[]>();
    for (const f of furnaces) {
      const r = f.region || "其他";
      const list = byRegion.get(r);
      if (list) list.push(f);
      else byRegion.set(r, [f]);
    }
    return Array.from(byRegion.entries()).map(([region, items]) => ({ region, items }));
  }, [furnaces]);

  const allIds = furnaces.map((f) => f.id);
  // 清响应：只丢弃预览结果，不动分页（切回重激活场景需保留用户所在页）。
  const clearPreviewResponses = () => {
    previewFreshnessRef.current = {};
    setPayrollSheet(null);
    setPayrollSheet2(null);
    setAnomalySheet(null);
    setTripartiteSheet(null);
    setFurnaceCount(0);
    setExportWarnings([]);
    setMonthlyHours(null);
    setOvertimeDetail(null);
  };
  // 完整重置：月份/工作点等参数变化时才把分页一起归位。
  const resetPreviews = () => {
    setRefreshInterrupted(false);
    setPreviewError(null);
    clearPreviewResponses();
    setPages(DEFAULT_PAGES);
  };
  const applySelection = (ids: number[]) => {
    setSelected(ids);
    try {
      localStorage.setItem(FURNACE_STORAGE_KEY, JSON.stringify(ids));
    } catch {
      /* ignore */
    }
    resetPreviews();
  };

  // 选中的工作点 id → 工作点编号(导出按 id 精确，避免跨地区同名工作点污染)
  const selectedNos = furnaces.filter((f) => selected.includes(f.id)).map((f) => f.furnaceNo);

  const canRun = !!month && selected.length > 0;
  const canManageMonthLock = role === "hr" || role === "admin";

  const toggleFurnace = (id: number) => {
    applySelection(selected.includes(id)
      ? selected.filter((selectedId) => selectedId !== id)
      : [...selected, id]);
  };

  // 参数指纹：预览返回时若参数已变则丢弃（防止过期结果覆盖新预览）
  const paramsKey = `${month}|${selected.join(",")}`;
  const paramsRef = useRef(paramsKey);
  paramsRef.current = paramsKey;
  const payroll2ParamsKey = [
    paramsKey,
    payroll2GroupBy.join(","),
    String(!payroll2HideZeroRows),
    String(payroll2MergeOvertimeOnlyRows),
    String(payroll2MergeSameSquadOvertimeOnlyRows),
  ].join("|");
  const payroll2ParamsRef = useRef(payroll2ParamsKey);
  payroll2ParamsRef.current = payroll2ParamsKey;

  const requestScope = useRef({ key: paramsKey, generation: 0 });
  if (requestScope.current.key !== paramsKey) {
    requestScope.current = { key: paramsKey, generation: requestScope.current.generation + 1 };
  }
  const runReport = <T,>(label: string, task: () => Promise<T>, requestKey = label,
                         valid: () => boolean = () => true): Promise<T> => {
    const generation = requestScope.current.generation;
    return reportQueue.run({
      key: `${generation}:${paramsKey}:${requestKey}`, label, task,
      current: () => mountedRef.current && activeRef.current
        && requestScope.current.generation === generation && paramsRef.current === paramsKey && valid(),
    });
  };

  const loadAbsenceOverrides = async (refreshChanged = false) => {
    const token = ++absenceRequestRef.current;
    try {
      const response = await runReport("读取手动旷工及可填上限", () => getAbsenceOverrides(month, selected), JSON.stringify(["getAbsenceOverrides", month, selected]));
      if (token === absenceRequestRef.current && paramsRef.current === paramsKey) {
        const snapshot = JSON.stringify(response.items);
        const changed = lastAbsenceRead.current?.key === paramsKey
          && lastAbsenceRead.current.snapshot !== snapshot;
        lastAbsenceRead.current = { key: paramsKey, snapshot };
        setAbsenceOverrides(response.items);
        setAbsenceLoadError("");
        // 切回时发现他人修改了共享值，更新已生成的报表，避免左右子列不同步。
        if (refreshChanged && changed && !manualSaveRef.current) {
          invalidate("/export");
          if (payrollSheet && !sectionLoading.payroll) await refreshPayroll(false);
          if (paramsRef.current !== paramsKey) return;
          if (payrollSheet2 && !sectionLoading.payroll2) await refreshPayroll2(payroll2GroupBy, !payroll2HideZeroRows,
            payroll2MergeOvertimeOnlyRows, payroll2MergeSameSquadOvertimeOnlyRows, false);
          if (paramsRef.current !== paramsKey) return;
          if (monthlyHours && !sectionLoading.hours) await refreshMonthlyHours(false);
        }
      }
    } catch (error) {
      if (isReportCancelled(error)) return;
      if (token === absenceRequestRef.current && paramsRef.current === paramsKey) {
        setAbsenceLoadError((error as Error).message || "手动旷工数据加载失败");
      }
    }
  };
  useEffect(() => {
    absenceRequestRef.current += 1;
    setAbsenceOverrides({});
    setAbsenceLoadError("");
    if (active && canRun) void loadAbsenceOverrides(true);
    // 按月份和工作点快照读取共享数据，切回页面重新读取，不使用页面缓存。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsKey, active]);

  const reportPreviewError = (error: unknown, fallback: string, key: string) => {
    if (paramsRef.current !== key || isReportCancelled(error)) return;
    const status = (error as { status?: number })?.status;
    const detail = error instanceof Error ? error.message : fallback;
    setPreviewError({ status, detail });
    if (status !== 422 && status !== 429) message.error(detail);
  };

  const setLoadingFor = (keys: SectionKey[], value: boolean) => {
    setSectionLoading((prev) => {
      const next = { ...prev };
      keys.forEach((key) => {
        next[key] = value;
      });
      return next;
    });
  };

  const setDownloadingFor = (key: SectionKey, value: boolean) => {
    setSectionDownloading((prev) => ({ ...prev, [key]: value }));
  };

  const updatePageSize = (key: SectionKey, size: number) => {
    setPageSizes((prev) => {
      if (prev[key] === size) return prev;
      const next = { ...prev, [key]: size };
      try {
        localStorage.setItem(PAGE_SIZE_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const tablePagination = (key: SectionKey, unit: string) => ({
    current: pages[key] || 1,
    pageSize: pageSizes[key] || 10,
    showSizeChanger: true,
    showQuickJumper: true,
    pageSizeOptions: [10, 20, 50, 100],
    showTotal: (t: number) => `共 ${t} ${unit}`,
    onChange: (page: number, size: number) => {
      setPages((prev) => (prev[key] === page ? prev : { ...prev, [key]: page }));
      updatePageSize(key, size);
    },
  });

  const markPreviewFresh = (section: SectionKey, key: string) => {
    previewFreshnessRef.current[section] = { epoch: getPageCacheEpoch(), paramsKey: key };
  };

  const refreshPayroll = async (bustCache = true) => {
    if (!canRun) return false;
    if (bustCache) { invalidate("/export"); void loadAbsenceOverrides(); }
    setPreviewError(null);
    setLoadingFor(["payroll"], true);
    const key = paramsKey;
    try {
      const m = await runReport("生成工作表预览", () => previewExportSheet(month, SECTION_LABEL.payroll, selectedNos, selected), JSON.stringify(["previewExportSheet", month, SECTION_LABEL.payroll, selectedNos, selected]));
      if (paramsRef.current === key) {
        setPayrollSheet(m.payrollSheet ?? null);
        markPreviewFresh("payroll", key);
        setFurnaceCount(m.furnaceCount);
        setExportWarnings(m.warnings ?? []);
      }
      return paramsRef.current === key;
    } catch (e) {
      reportPreviewError(e, "输出给薪资预览失败", paramsKey);
      return false;
    } finally {
      setLoadingFor(["payroll"], false);
    }
  };

  const refreshPayroll2 = async (
    groupBy = payroll2GroupBy,
    includeZeroRows = !payroll2HideZeroRows,
    mergeOvertimeOnlyRows = payroll2MergeOvertimeOnlyRows,
    mergeSameSquadOvertimeOnlyRows = payroll2MergeSameSquadOvertimeOnlyRows,
    bustCache = true,
  ) => {
    if (!canRun) return false;
    if (bustCache) { invalidate("/export"); void loadAbsenceOverrides(); }
    setPreviewError(null);
    setLoadingFor(["payroll2"], true);
    const key = [
      paramsKey,
      groupBy.join(","),
      String(includeZeroRows),
      String(mergeOvertimeOnlyRows),
      String(mergeSameSquadOvertimeOnlyRows),
    ].join("|");
    try {
      const m = await runReport("生成工作表预览", () => previewExportSheet(
        month,
        SECTION_LABEL.payroll2,
        selectedNos,
        selected,
        undefined,
        groupBy,
        includeZeroRows,
        mergeOvertimeOnlyRows,
        mergeSameSquadOvertimeOnlyRows,
      ), JSON.stringify(["previewExportSheet",
        month,
        SECTION_LABEL.payroll2,
        selectedNos,
        selected,
        undefined,
        groupBy,
        includeZeroRows,
        mergeOvertimeOnlyRows,
        mergeSameSquadOvertimeOnlyRows,
      ]), () => payroll2ParamsRef.current === key);
      if (payroll2ParamsRef.current === key) {
        setPayrollSheet2(m.payrollSheet ?? null);
        markPreviewFresh("payroll2", key);
        setFurnaceCount(m.furnaceCount);
        setExportWarnings(m.warnings ?? []);
      }
      return payroll2ParamsRef.current === key;
    } catch (e) {
      reportPreviewError(e, "输出给薪资2预览失败", paramsKey);
      return false;
    } finally {
      setLoadingFor(["payroll2"], false);
    }
  };

  const refreshExtraSheet = async (
    sectionKey: "anomaly" | "tripartite",
    bustCache = true,
  ) => {
    if (!canRun) return false;
    if (bustCache) { invalidate("/export"); void loadAbsenceOverrides(); }
    setPreviewError(null);
    setLoadingFor([sectionKey], true);
    const key = paramsKey;
    try {
      const m = await runReport("生成工作表预览", () => previewExportSheet(month, SECTION_LABEL[sectionKey], selectedNos, selected), JSON.stringify(["previewExportSheet", month, SECTION_LABEL[sectionKey], selectedNos, selected]));
      if (paramsRef.current === key) {
        markPreviewFresh(sectionKey, key);
        if (sectionKey === "anomaly") setAnomalySheet(m.extraSheet ?? null);
        else setTripartiteSheet(m.extraSheet ?? null);
        setFurnaceCount(m.furnaceCount);
        setExportWarnings(m.warnings ?? []);
      }
      return paramsRef.current === key;
    } catch (e) {
      reportPreviewError(e, `${SECTION_LABEL[sectionKey]}预览失败`, paramsKey);
      return false;
    } finally {
      setLoadingFor([sectionKey], false);
    }
  };

  const refreshMonthlyHours = async (bustCache = true) => {
    if (!canRun) return false;
    if (bustCache) { invalidate("/export"); void loadAbsenceOverrides(); }
    setPreviewError(null);
    setLoadingFor(["hours"], true);
    const key = paramsKey;
    try {
      const m = await runReport("计算班次有效工时", () => previewMonthlyHours(month, selected), JSON.stringify(["previewMonthlyHours", month, selected]));
      if (paramsRef.current === key) {
        setMonthlyHours(m);
        markPreviewFresh("hours", key);
      }
      return paramsRef.current === key;
    } catch (e) {
      reportPreviewError(e, "班次有效工时预览失败", paramsKey);
      return false;
    } finally {
      setLoadingFor(["hours"], false);
    }
  };

  const refreshOvertimeDetail = async (bustCache = true) => {
    if (!canRun) return false;
    if (bustCache) { invalidate("/export"); void loadAbsenceOverrides(); }
    setPreviewError(null);
    const keys: SectionKey[] = ["overtime", "overtimeSummary", "overtimePerson"];
    setLoadingFor(keys, true);
    const key = paramsKey;
    try {
      const m = await runReport("计算加班明细", () => previewOvertimeDetail(month, selected), JSON.stringify(["previewOvertimeDetail", month, selected]));
      if (paramsRef.current === key) {
        setOvertimeDetail(m);
        markPreviewFresh("overtime", key);
      }
      return paramsRef.current === key;
    } catch (e) {
      reportPreviewError(e, "加班表预览失败", paramsKey);
      return false;
    } finally {
      setLoadingFor(keys, false);
    }
  };

  // 串行刷新链令牌：新链启动即作废旧链，同一时刻最多一条链压向后端重表计算。
  const refreshChainRef = useRef(0);

  const refreshAllSequential = async (bustCache = true) => {
    if (!canRun || fullRefreshRef.current) return;
    fullRefreshRef.current = true;
    setFullRefreshing(true);
    setFullProgress(0);
    refreshChainRef.current += 1;
    const chainToken = refreshChainRef.current;
    setRefreshInterrupted(false);
    setPreviewError(null);
    let completed = false;
    if (bustCache) { invalidate("/export"); void loadAbsenceOverrides(); }
    // 预览结果不预先清空：保留旧展示直到新结果返回覆盖，避免重表计算期间整页空白。
    try {
      if (!(await refreshPayroll(false))) return;
      setFullProgress(1);
      if (refreshChainRef.current !== chainToken || paramsRef.current !== paramsKey) return;
      if (!(await refreshPayroll2(
        payroll2GroupBy,
        !payroll2HideZeroRows,
        payroll2MergeOvertimeOnlyRows,
        payroll2MergeSameSquadOvertimeOnlyRows,
        false,
      ))) return;
      setFullProgress(2);
      if (refreshChainRef.current !== chainToken || paramsRef.current !== paramsKey) return;
      if (!(await refreshExtraSheet("anomaly", false))) return;
      setFullProgress(3);
      if (refreshChainRef.current !== chainToken || paramsRef.current !== paramsKey) return;
      if (!(await refreshExtraSheet("tripartite", false))) return;
      setFullProgress(4);
      if (refreshChainRef.current !== chainToken || paramsRef.current !== paramsKey) return;
      if (!(await refreshMonthlyHours(false))) return;
      setFullProgress(5);
      if (refreshChainRef.current !== chainToken || paramsRef.current !== paramsKey) return;
      if (!(await refreshOvertimeDetail(false))) return;
      setFullProgress(6);
      if (refreshChainRef.current !== chainToken || paramsRef.current !== paramsKey) return;
      completed = true;
    } finally {
      fullRefreshRef.current = false;
      setFullRefreshing(false);
      if (refreshChainRef.current === chainToken) {
        if (paramsRef.current === paramsKey) setRefreshInterrupted(!completed);
        setLoadingFor(
          ["payroll", "payroll2", "anomaly", "tripartite", "hours", "overtime", "overtimeSummary", "overtimePerson"],
          false,
        );
      }
    }
  };

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (!becameActive) return;
    const epoch = getPageCacheEpoch();
    const tasks: [SectionKey, () => Promise<boolean>][] = [
      ["payroll", () => refreshPayroll(false)],
      ["payroll2", () => refreshPayroll2(payroll2GroupBy, !payroll2HideZeroRows,
        payroll2MergeOvertimeOnlyRows, payroll2MergeSameSquadOvertimeOnlyRows, false)],
      ["anomaly", () => refreshExtraSheet("anomaly", false)],
      ["tripartite", () => refreshExtraSheet("tripartite", false)],
      ["hours", () => refreshMonthlyHours(false)],
      ["overtime", () => refreshOvertimeDetail(false)],
    ];
    const staleTasks = tasks.filter(([section]) => {
      const last = previewFreshnessRef.current[section];
      const key = section === "payroll2" ? payroll2ParamsKey : paramsKey;
      return last?.paramsKey === key && !sectionLoading[section]
        && last.epoch !== epoch;
    });
    // 首次进入仍需用户手动生成。只刷新曾成功加载且因数据写入失效的表，不额外计算未查看的表。
    // 保留原展示、分页、关键字和工作点选择，响应成功后替换；失败由现有错误提示处理。
    if (staleTasks.length) invalidate("/export");
    void getFurnaces().then((r) => setFurnaces(r.items)).catch(() => undefined);
    void getExportButtonVisibility()
      .then((r) => setExportButtonVisibilityState(r.buttons)).catch(() => undefined);
    const referenceMonth = monthRef.current;
    if (referenceMonth) {
      void getMonthlyExportLock(referenceMonth).then((value) => {
        if (monthRef.current === referenceMonth) setMonthLocks(value);
      }).catch(() => undefined);
    }
    if (staleTasks.length) {
      const chainToken = ++refreshChainRef.current;
      void (async () => {
        for (const [, refresh] of staleTasks) {
          if (refreshChainRef.current !== chainToken || paramsRef.current !== paramsKey) return;
          if (!(await refresh())) return;
        }
      })();
    }
    // 切回时使用当前筛选快照；普通渲染不会自动启动重表请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const refreshAndFilter = async (sectionKey: FilterableSectionKey) => {
    if (!canRun || sectionLoading[sectionKey]) return;
    applyKeywordFilter(sectionKey);
    // 其他页面可能已经改动业务数据；先废弃本页 sessionStorage 响应缓存再按当前条件回源。
    invalidate("/export");
    switch (sectionKey) {
      case "payroll":
        await refreshPayroll(false);
        break;
      case "payroll2":
        await refreshPayroll2(
          payroll2GroupBy,
          !payroll2HideZeroRows,
          payroll2MergeOvertimeOnlyRows,
          payroll2MergeSameSquadOvertimeOnlyRows,
          false,
        );
        break;
      case "anomaly":
      case "tripartite":
        await refreshExtraSheet(sectionKey, false);
        break;
      case "hours":
        await refreshMonthlyHours(false);
        break;
      case "overtime":
      case "overtimePerson":
        await refreshOvertimeDetail(false);
        break;
    }
  };

  // 无需先生成预览；下载时旷工优先采用已保存的手动值，预览保留自动计算值。
  const onExport = async () => {
    if (!canRun) return;
    setDownloading(true);
    try {
      const name = await runReport("下载合并报表", () => downloadExport(
        month,
        selectedNos,
        selected,
        payroll2GroupBy,
        !payroll2HideZeroRows,
        payroll2MergeOvertimeOnlyRows,
        payroll2MergeSameSquadOvertimeOnlyRows,
      ), JSON.stringify(["downloadExport",
        month,
        selectedNos,
        selected,
        payroll2GroupBy,
        !payroll2HideZeroRows,
        payroll2MergeOvertimeOnlyRows,
        payroll2MergeSameSquadOvertimeOnlyRows,
      ]));
      message.success(`已导出 ${name}`);
    } catch (e) {
      if (!isReportCancelled(e)) message.error((e as Error)?.message || "导出失败");
    } finally {
      setDownloading(false);
    }
  };

  const exportSheet = async (key: SectionKey) => {
    if (!canRun) return;
    const sheet = SECTION_LABEL[key];
    setDownloadingFor(key, true);
    try {
      const name = await runReport("下载工作表", () => downloadExportSheet(
        month,
        sheet,
        selectedNos,
        selected,
        payroll2GroupBy,
        !payroll2HideZeroRows,
        payroll2MergeOvertimeOnlyRows,
        payroll2MergeSameSquadOvertimeOnlyRows,
      ), JSON.stringify(["downloadExportSheet",
        month,
        sheet,
        selectedNos,
        selected,
        payroll2GroupBy,
        !payroll2HideZeroRows,
        payroll2MergeOvertimeOnlyRows,
        payroll2MergeSameSquadOvertimeOnlyRows,
      ]));
      message.success(`已导出 ${name}`);
    } catch (e) {
      if (!isReportCancelled(e)) message.error((e as Error)?.message || "导出失败");
    } finally {
      setDownloadingFor(key, false);
    }
  };

  const updatePayroll2GroupBy = (values: string[]) => {
    const next = ["工号", ...values.filter((x) => x !== "工号" && PAYROLL2_GROUP_FIELDS.includes(x as typeof PAYROLL2_GROUP_FIELDS[number]))];
    setPayroll2GroupBy(next);
    setPayrollSheet2(null);
    setPages((prev) => ({ ...prev, payroll2: 1 }));
    try {
      localStorage.setItem(PAYROLL2_GROUP_STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* ignore */
    }
    void refreshPayroll2(
      next,
      !payroll2HideZeroRows,
      payroll2MergeOvertimeOnlyRows,
      payroll2MergeSameSquadOvertimeOnlyRows,
    );
  };

  const updatePayroll2HideZeroRows = (checked: boolean) => {
    setPayroll2HideZeroRows(checked);
    setPayrollSheet2(null);
    setPages((prev) => ({ ...prev, payroll2: 1 }));
    try {
      localStorage.setItem(PAYROLL2_HIDE_ZERO_ROWS_STORAGE_KEY, checked ? "1" : "0");
    } catch {
      /* ignore */
    }
    void refreshPayroll2(
      payroll2GroupBy,
      !checked,
      payroll2MergeOvertimeOnlyRows,
      payroll2MergeSameSquadOvertimeOnlyRows,
    );
  };

  const updatePayroll2MergeOvertimeOnlyRows = (checked: boolean) => {
    setPayroll2MergeOvertimeOnlyRows(checked);
    setPayrollSheet2(null);
    setPages((prev) => ({ ...prev, payroll2: 1 }));
    try {
      localStorage.setItem(
        PAYROLL2_MERGE_OVERTIME_ONLY_ROWS_STORAGE_KEY,
        checked ? "1" : "0",
      );
    } catch {
      /* ignore */
    }
    void refreshPayroll2(
      payroll2GroupBy,
      !payroll2HideZeroRows,
      checked,
      payroll2MergeSameSquadOvertimeOnlyRows,
    );
  };

  const updatePayroll2MergeSameSquadOvertimeOnlyRows = (checked: boolean) => {
    setPayroll2MergeSameSquadOvertimeOnlyRows(checked);
    setPayrollSheet2(null);
    setPages((prev) => ({ ...prev, payroll2: 1 }));
    try {
      localStorage.setItem(
        PAYROLL2_MERGE_SAME_SQUAD_OVERTIME_ONLY_ROWS_STORAGE_KEY,
        checked ? "1" : "0",
      );
    } catch {
      /* ignore */
    }
    void refreshPayroll2(
      payroll2GroupBy,
      !payroll2HideZeroRows,
      payroll2MergeOvertimeOnlyRows,
      checked,
    );
  };

  const toggleMonthLocks = async (furnaceIds: number[], locked: boolean, loadingKey: string) => {
    if (!month || !furnaceIds.length || lockLoadingKey) return;
    setLockLoadingKey(loadingKey);
    try {
      const next = await setMonthlyExportLock(month, furnaceIds, locked);
      // 写请求飞行期间允许切月：响应属于点击时的月份，不能覆盖当前选中月份的
      // 锁状态展示（旧月状态错配到新月会让 HR 误判关账）。响应体自带 month，
      // 不一致时改为重拉当前月份；yearLocks 的汇总按响应月份落位不受影响。
      if (next.month === monthRef.current) {
        setMonthLocks(next);
      } else {
        getMonthlyExportLock(monthRef.current)
          .then(setMonthLocks)
          .catch(() => setMonthLocks(null));
      }
      setYearLocks((items) => {
        const summary = {
          month: next.month,
          lockedCount: next.lockedCount,
          totalCount: next.totalCount,
        };
        const found = items.some((item) => item.month === summary.month);
        if (found) return items.map((item) => (item.month === summary.month ? summary : item));
        return [...items, summary].sort((a, b) => a.month.localeCompare(b.month));
      });
      void loadAbsenceOverrides();
      message.success(locked ? `已锁定 ${furnaceIds.length} 个工作点` : `已解锁 ${furnaceIds.length} 个工作点`);
    } catch (e) {
      message.error((e as Error)?.message || "操作失败");
    } finally {
      setLockLoadingKey(null);
    }
  };

  const hasPreview = !!(payrollSheet || payrollSheet2 || anomalySheet || tripartiteSheet || monthlyHours || overtimeDetail);
  const emptyText = (msg: string) =>
    !canRun ? "请先选择月份与工作点" : !hasPreview ? "请点击「生成全部预览」" : msg;

  const canSeeExportButton = (key: ExportButtonKey) => {
    if (role === "admin") return true;
    if (!visibilityLoaded || (role !== "hr" && role !== "stat")) return false;
    return exportButtonVisibility[key].includes(role);
  };

  const toggleExportButtonRole = async (
    key: ExportButtonKey,
    targetRole: ExportVisibleRole,
    checked: boolean,
  ) => {
    if (role !== "admin" || visibilitySaving) return;
    const previous = exportButtonVisibility;
    const current = previous[key];
    const roles = checked
      ? (["hr", "stat"].filter((r) => r === targetRole || current.includes(r as ExportVisibleRole)) as ExportVisibleRole[])
      : current.filter((r) => r !== targetRole);
    const next = { ...previous, [key]: roles };
    setExportButtonVisibilityState(next);
    setVisibilitySaving(true);
    try {
      const saved = await setExportButtonVisibility(next);
      setExportButtonVisibilityState(saved.buttons);
      message.success("导出按钮可见角色已更新");
    } catch (e) {
      setExportButtonVisibilityState(previous);
      message.error((e as Error)?.message || "保存可见角色失败");
    } finally {
      setVisibilitySaving(false);
    }
  };

  const VisibilityCheckboxes = ({ buttonKey }: { buttonKey: ExportButtonKey }) =>
    role === "admin" ? (
      <Space size={6} data-testid={`export-visibility-${buttonKey}`}>
        <span style={{ color: "#999", fontSize: 12 }}>可见：</span>
        <Checkbox
          checked={exportButtonVisibility[buttonKey].includes("hr")}
          disabled={visibilitySaving}
          onChange={(e) => void toggleExportButtonRole(buttonKey, "hr", e.target.checked)}
        >
          HR
        </Checkbox>
        <Checkbox
          checked={exportButtonVisibility[buttonKey].includes("stat")}
          disabled={visibilitySaving}
          onChange={(e) => void toggleExportButtonRole(buttonKey, "stat", e.target.checked)}
        >
          统计
        </Checkbox>
      </Space>
    ) : null;

  const toolbar = (
    <Space wrap>
      <Button
        data-testid="export-preview-btn"
        disabled={!canRun || fullRefreshing || Object.values(sectionLoading).some(Boolean)}
        onClick={() => refreshAllSequential()}
      >
        {fullRefreshing ? `正在刷新 ${fullProgress}/6` : hasPreview ? "刷新全部预览" : "生成全部预览"}
      </Button>
      {canSeeExportButton("combined") && (
        <Button
          type="primary"
          data-testid="export-btn"
          loading={downloading}
          disabled={!canRun}
          onClick={() => onExport()}
        >
          导出合并 Excel
        </Button>
      )}
      <VisibilityCheckboxes buttonKey="combined" />
      {canSeeExportButton("combined") && (
        <Tooltip title="导出时旷工优先采用已保存的手动值；页面自动计算列始终保留系统计算值">
          <span style={{ color: "#999", fontSize: 12 }}>
            下方所有表会导出到同一个 Excel 文件
          </span>
        </Tooltip>
      )}
    </Space>
  );

  const saveManualAbsence = async (empId: string, days: number | null, revision: number) => {
    if (manualSaveRef.current) return;
    manualSaveRef.current = true;
    setManualSaving(true);
    absenceRequestRef.current += 1;
    try {
      const saved = await runReport("保存手动旷工", () => saveAbsenceOverride(month, empId, days, revision), JSON.stringify(["saveAbsenceOverride", month, empId, days, revision]));
      if (paramsRef.current !== paramsKey) return;
      setAbsenceOverrides((current) => ({ ...current, [empId]: saved }));
      invalidate("/export");
      message.success(days === null ? "已清空手动值，导出使用自动计算值" : "手动旷工已保存，所有用户共享");
      // 只更新已生成的含旷工报表，导出始终从后端读取已保存值。
      ++refreshChainRef.current;
      if (payrollSheet) await refreshPayroll(false);
      if (paramsRef.current !== paramsKey) return;
      if (payrollSheet2) await refreshPayroll2(payroll2GroupBy, !payroll2HideZeroRows,
        payroll2MergeOvertimeOnlyRows, payroll2MergeSameSquadOvertimeOnlyRows, false);
      if (paramsRef.current !== paramsKey) return;
      if (monthlyHours) await refreshMonthlyHours(false);
    } catch (error) {
      if (isReportCancelled(error)) return;
      message.error((error as Error).message || "保存失败");
      if (paramsRef.current === paramsKey) void loadAbsenceOverrides();
    } finally {
      manualSaveRef.current = false;
      setManualSaving(false);
    }
  };
  const manualAbsenceCell = (empId: unknown) => {
    const id = String(empId ?? "").replace(/^'/, "");
    const value = absenceOverrides[id];
    const busy = manualSaving || Object.values(sectionLoading).some(Boolean);
    return <ManualAbsenceEditor key={`${month}:${id}`} value={value && {
      ...value, editable: value.editable && !busy,
    }}
      onSave={(days, revision) => saveManualAbsence(id, days, revision)} />;
  };

  // ---- 薪资预览列 ----
  const payrollPreviewCols = useMemo(() => {
    const dynamicOt =
      payrollSheet?.columns.filter((c) => /^加班(?:标准)?\d+$/.test(c)) ?? [];
    return [...PAYROLL_PREVIEW_BASE_COLS, ...dynamicOt];
  }, [payrollSheet]);
  const payrollColumns = payrollPreviewCols.map((col) => {
    // Ant Table 只对当前 dataSource 的展示副本排序；下载仍重新请求后端，绝不读取表格排序结果。
    const base = {
      title: col,
      dataIndex: col,
      key: col,
      width: 110,
      ellipsis: true,
      sorter: (a: PayrollRow, b: PayrollRow) =>
        comparePreviewValues(a[col], b[col]),
    };
    switch (col) {
      case "用工类型":
        return {
          ...base,
          width: 90,
          render: (v: string | number) =>
            v ? <Tag color={EMPLOY_TYPE_COLOR[String(v)] || "default"}>{String(v)}</Tag> : "-",
        };
      case "薪资比例系数":
        return { ...base, width: 100, render: (v: string | number) => ratioText(v) };
      case "实际出勤天数":
        return {
          ...base,
          width: 110,
          render: (v: string | number, row: PayrollRow) => {
            const actual = Number(v);
            const expect = Number(row["应出勤天数"]);
            const missing = Number.isFinite(actual) && Number.isFinite(expect) && actual < expect;
            return (
              <span style={{ color: missing ? "#dc2626" : undefined, fontWeight: missing ? 600 : 400 }}>
                {Number.isFinite(actual) ? actual : v}
              </span>
            );
          },
        };
      case "旷工天数":
        return {
          ...base,
          width: 260,
          children: [
            { ...base, title: "自动计算", width: 90, render: (v: string | number) => {
              const value = absenceValue(v);
              const n = Number(value);
              return Number.isFinite(n) && n > 0 ? <Tag color="red">{n}</Tag> : value;
            } },
            { title: "手动输入", key: "absence-manual", width: 170,
              render: (_: unknown, row: PayrollRow) => manualAbsenceCell(row["工号"]) },
          ],
        };
      default:
        return base;
    }
  });
  const payroll2Columns = (payrollSheet2?.columns ?? []).map((col) => ({
    title: col,
    dataIndex: col,
    key: col,
    width: col === "行名" ? 220 : col.startsWith("岗位") ? 120 : 100,
    ellipsis: true,
    sorter: (a: PayrollRow, b: PayrollRow) =>
      comparePreviewValues(a[col], b[col]),
    render: (v: string | number | undefined) => cellText(col === "旷工天数" ? absenceValue(v) : v),
  })).map((column) => column.key !== "旷工天数" ? column : {
    title: "旷工天数", key: "旷工天数", children: [
      { ...column, title: "自动计算" },
      { title: "手动输入", key: "absence-manual", width: 170,
        render: (_: unknown, row: PayrollRow) => manualAbsenceCell(row["工号"]) },
    ],
  });

  // ---- 异常表 / 三方对账表列（按 sheet.columns 动态生成）----
  const buildExtraColumns = (cols: string[]) =>
    cols.map((c, i) => ({
      title: c,
      key: c,
      width: 120,
      ellipsis: true,
      sorter: (
        a: Record<string, string | number>,
        b: Record<string, string | number>,
      ) => comparePreviewValues(a[c], b[c]),
      render: (_: unknown, row: Record<string, string | number>) => {
        const v = row[c] ?? (row as unknown as (string | number)[])[i];
        if (c === "旷工天数") return cellText(absenceValue(v));
        return v === "" || v == null ? <span style={{ color: "#bbb" }}>-</span> : String(v);
      },
    }));

  const anomalyRows = anomalySheet ? toObjects(anomalySheet) : [];
  const tripartiteRows = tripartiteSheet ? toObjects(tripartiteSheet) : [];

  // ---- 预览表「工号/姓名」筛选后的展示行（不影响导出：导出函数仍读取上面未过滤的原始行/sheet）----
  const payrollRowsShown = useMemo(
    () => filterObjectRows(payrollSheet?.rows ?? [], kw.applied.payroll),
    [payrollSheet, kw.applied.payroll],
  );
  const payroll2RowsShown = useMemo(
    () => filterObjectRows(payrollSheet2?.rows ?? [], kw.applied.payroll2),
    [payrollSheet2, kw.applied.payroll2],
  );
  const anomalyRowsShown = useMemo(
    () => filterObjectRows(anomalyRows, kw.applied.anomaly),
    [anomalyRows, kw.applied.anomaly],
  );
  const tripartiteRowsShown = useMemo(
    () => filterObjectRows(tripartiteRows, kw.applied.tripartite),
    [tripartiteRows, kw.applied.tripartite],
  );
  const hoursRowsShown = useMemo(
    () => filterGridRows(monthlyHours?.rows, monthlyHours?.columns, kw.applied.hours),
    [monthlyHours, kw.applied.hours],
  );
  const overtimeRowsShown = useMemo(
    () => filterGridRows(overtimeDetail?.rows, overtimeDetail?.columns, kw.applied.overtime),
    [overtimeDetail, kw.applied.overtime],
  );
  const overtimePersonRowsShown = useMemo(
    () => filterGridRows(overtimeDetail?.byPersonRows, overtimeDetail?.byPersonColumns, kw.applied.overtimePerson),
    [overtimeDetail, kw.applied.overtimePerson],
  );

  // ---- 班次有效工时列（6 固定列 + 日历日格窄列 + 汇总列）----
  const hoursColumns = (() => {
    if (!monthlyHours) return [];
    return buildGridColumns(monthlyHours.columns, (c, i) => {
      if (i < 6) return 90;
      if (c === "全月出勤" || c === "旷工天数" || c === "加班合计") return 90;
      if (c === "年休假统计天数") return 120;
      return 48; // 日历日格
    }).map((column) => column.title !== "旷工天数" ? column : {
      title: "旷工天数", key: "旷工天数", children: [
        { ...column, title: "自动计算" },
        { title: "手动输入", key: "absence-manual", width: 170,
          render: (_: unknown, row: GridRow) => manualAbsenceCell(row[monthlyHours.columns.indexOf("工号")]) },
      ],
    });
  })();

  const renderPreviewActions = (sectionKey: SectionKey, onRefresh: () => void) => (
    <Space wrap>
      <Button size="small" loading={sectionLoading[sectionKey]} disabled={!canRun || fullRefreshing || sectionLoading[sectionKey]} onClick={onRefresh}>
        刷新预览
      </Button>
      {canSeeExportButton(sectionKey) && (
        <Button
          size="small"
          data-testid={`export-sheet-${sectionKey}`}
          loading={sectionDownloading[sectionKey]}
          disabled={!canRun}
          onClick={() => exportSheet(sectionKey)}
        >
          导出
        </Button>
      )}
      <VisibilityCheckboxes buttonKey={sectionKey} />
    </Space>
  );

  // 作为普通 render helper 返回稳定的 Input element.type；不要改成 ExportPage 内联 JSX 组件。
  const renderFilterControls = (filterKey: FilterableSectionKey) => (
    <Space style={{ marginBottom: 10 }} wrap>
      <Input
        allowClear
        size="small"
        placeholder="工号或姓名"
        style={{ width: 180 }}
        data-testid={`filter-kw-${filterKey}`}
        value={kw.drafts[filterKey]}
        onChange={(e) => kw.setDraft(filterKey, e.target.value)}
        onPressEnter={() => applyKeywordFilter(filterKey)}
      />
      <Button
        size="small"
        data-testid={`filter-apply-${filterKey}`}
        onClick={() => applyKeywordFilter(filterKey)}
      >
        筛选
      </Button>
      <Button
        size="small"
        type="primary"
        data-testid={`filter-refresh-${filterKey}`}
        loading={sectionLoading[filterKey]}
        disabled={!canRun}
        onClick={() => void refreshAndFilter(filterKey)}
      >
        刷新并筛选
      </Button>
      {kw.applied[filterKey] && (
        <Button size="small" type="link" onClick={() => clearKeywordFilter(filterKey)}>
          清空筛选
        </Button>
      )}
      <span style={{ color: "#999", fontSize: 12 }}>
        仅影响本表展示，不影响导出内容；其他页面更新数据后，请点击“刷新并筛选”。
      </span>
    </Space>
  );

  const grid = (sectionKey: SectionKey, cols: string[] | undefined, rows: GridRow[] | undefined, empty: string, unit: string) => (
    <Table<GridRow>
      rowKey={(_r, i) => String(i)}
      size="small"
      loading={sectionLoading[sectionKey]}
      columns={buildGridColumns(cols ?? [])}
      dataSource={rows ?? []}
      scroll={{ x: "max-content" }}
      pagination={tablePagination(sectionKey, unit)}
      locale={{ emptyText: emptyText(empty) }}
    />
  );

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="large">
      <Card size="small" title="报表请求状态" data-testid="export-request-status">
        <Space direction="vertical" style={{ width: "100%" }}>
          {reportStatusError && <Alert type="warning" message={reportStatusError} />}
          <div aria-live="polite">
            {queueStatus.pending > 0
              ? `${queueStatus.waiting ? "正在等待其他计算完成，系统会自动重试" : "正在处理"}：${queueStatus.label || "请求排队中"}（本页待完成 ${queueStatus.pending} 项）`
              : "本页没有等待中的请求"}
            {fullRefreshing && ` · 已更新 ${fullProgress}/6 组预览`}
          </div>
          {reportStatus?.paused ? <Tag color="orange">报表维护中</Tag> : null}
          {reportStatus?.items?.length ? reportStatus.items.map((item, index) => (
            <div key={`${item.userId}:${item.requestedAt}:${index}`}>
              <Tag color="processing">正在计算</Tag>
              <b>{item.userName}</b>（账号：{item.userId}） · {item.task}
              <span> · 发起时间：{dayjs(item.requestedAt).format("YYYY-MM-DD HH:mm:ss")}</span>
              <span> · 已运行 {Math.floor(item.elapsedSeconds)} 秒</span>
            </div>
          )) : <span style={{ color: "#777" }}>{reportStatusError ? "" : reportStatus ? "当前没有用户占用计算名额" : "正在读取占用状态…"}</span>}
          <span style={{ color: "#999", fontSize: 12 }}>每3秒更新；可按上方姓名或账号联系正在计算的用户。</span>
        </Space>
      </Card>
      {absenceLoadError && <Alert type="error" showIcon message={absenceLoadError}
        action={<Button size="small" onClick={() => void loadAbsenceOverrides()}>重试加载手动旷工</Button>} />}
      <Card title="月度导出" data-testid="export-title-card">
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <div>
            <label>月份（可选择当前月及历史月份）</label>
            <DatePicker
              picker="month"
              allowClear={false}
              data-testid="export-month"
              value={month ? dayjs(`${month}-01`) : undefined}
              // 封账请求飞行期间禁止切月，杜绝旧月锁状态回包覆盖新月展示
              disabled={!!lockLoadingKey}
              disabledDate={(date) => Boolean(
                anchorMonth && date.isAfter(dayjs(`${anchorMonth}-01`), "month")
              )}
              onChange={(_date, value) => {
                if (value) applyMonth(value as string);
              }}
              style={{ width: 240, marginTop: 8 }}
            />
            <div style={{ color: "#999", fontSize: 12, marginTop: 8 }}>
              快捷选择：前 6 个月至当前月，最后一个为当前月。
            </div>
            <div
              data-testid="export-year-months"
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(92px, 1fr))",
                gap: 8,
                marginTop: 8,
              }}
            >
              {visibleMonths.map((m) => {
                const lock = yearLockByMonth.get(m);
                const active = m === month;
                return (
                  <Button
                    key={m}
                    size="small"
                    type={active ? "primary" : "default"}
                    data-testid={`export-month-${m}`}
                    disabled={!!lockLoadingKey}
                    onClick={() => applyMonth(m)}
                    style={{ height: 34, paddingInline: 6 }}
                  >
                    <Space size={4}>
                      <span>{m.slice(0, 4)}年{Number(m.slice(5, 7))}月</span>
                      {!!lock?.lockedCount && (
                        <Tooltip
                          title={
                            lock.lockedCount >= lock.totalCount
                              ? "本月全部工作点已封账"
                              : `本月存在未封账工作点（${lock.lockedCount}/${lock.totalCount}），封账后新建工作点默认继承锁定`
                          }
                        >
                          <Tag style={{ ...SOFT_LOCK_TAG_STYLE, marginInlineEnd: 0, fontSize: 11 }}>
                            {lock.lockedCount}/{lock.totalCount}锁
                          </Tag>
                        </Tooltip>
                      )}
                    </Space>
                  </Button>
                );
              })}
            </div>
          </div>
          <div>
            <label>工作点（多选，先选范围再生成预览）</label>
            <Space size="small" style={{ marginTop: 6, flexWrap: "wrap" }}>
              <Tag color="blue">导出范围</Tag>
              <Button size="small" onClick={() => applySelection(allIds)}>全选</Button>
              <Button
                size="small"
                onClick={() => applySelection(allIds.filter((id) => !selected.includes(id)))}
              >
                反选
              </Button>
              <Button size="small" onClick={() => applySelection([])}>清空</Button>
              <span style={{ color: "#999", fontSize: 12 }}>
                已选 {selected.length}/{furnaces.length}
              </span>
            </Space>
            <div style={{ color: "#999", fontSize: 12, marginTop: 6 }}>
              蓝色工作点按钮只控制导出范围；右侧小锁只控制该工作点当月封账，两者互不影响。
            </div>
            <div data-testid="export-furnace" style={{ display: "grid", gap: 12, marginTop: 10 }}>
              {furnaceRegionGroups.map(({ region, items }) => {
                const regionIds = items.map((item) => item.id);
                const selectedCount = regionIds.filter((id) => selected.includes(id)).length;
                const allRegionSelected = selectedCount === regionIds.length;
                const regionLockedCount = regionIds.filter((id) => monthLockByFurnace.get(id)?.locked).length;
                return (
                  <div key={region} style={{ border: "1px solid #d9d9d9", borderRadius: 8, padding: 12 }}>
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: 10,
                        flexWrap: "wrap",
                        marginBottom: 10,
                      }}
                    >
                      <Space wrap>
                        <strong>{region}</strong>
                        <Tag color="blue">导出已选 {selectedCount}/{items.length}</Tag>
                        <Button
                          size="small"
                          type="link"
                          onClick={() => applySelection(allRegionSelected
                            ? selected.filter((id) => !regionIds.includes(id))
                            : [...new Set([...selected, ...regionIds])])}
                        >
                          {allRegionSelected ? "导出：清空本地区" : "导出：全选本地区"}
                        </Button>
                      </Space>
                      <Space size={6}>
                        <Tag style={regionLockedCount ? SOFT_LOCK_TAG_STYLE : undefined}>
                          封账 {regionLockedCount}/{items.length}
                        </Tag>
                        {canManageMonthLock && (
                          <>
                            <Popconfirm
                              title={`锁定 ${month} ${region}全部 ${items.length} 个工作点？`}
                              description="锁定后，这些工作点当月会影响导出的写操作将被拦截。"
                              onConfirm={() => void toggleMonthLocks(regionIds, true, `region-lock:${region}`)}
                            >
                              <Button
                                size="small"
                                icon={<LockFilled style={SOFT_LOCK_ICON_STYLE} />}
                                disabled={regionLockedCount === items.length || !!lockLoadingKey}
                                loading={lockLoadingKey === `region-lock:${region}`}
                              >
                                一键全锁
                              </Button>
                            </Popconfirm>
                            <Popconfirm
                              title={`解锁 ${month} ${region}全部工作点？`}
                              description="解锁只移除月度封账，不会改变导出选择或值班长日确认锁。"
                              onConfirm={() => void toggleMonthLocks(regionIds, false, `region-unlock:${region}`)}
                            >
                              <Button
                                size="small"
                                icon={<UnlockOutlined style={UNLOCKED_ICON_STYLE} />}
                                disabled={regionLockedCount === 0 || !!lockLoadingKey}
                                loading={lockLoadingKey === `region-unlock:${region}`}
                              >
                                一键全解锁
                              </Button>
                            </Popconfirm>
                          </>
                        )}
                      </Space>
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(130px, 1fr))", gap: 8 }}>
                      {items.map((furnace) => {
                        const active = selected.includes(furnace.id);
                        const furnaceLocked = !!monthLockByFurnace.get(furnace.id)?.locked;
                        const loadingKey = `furnace:${furnace.id}`;
                        return (
                          <div key={furnace.id} style={{ display: "flex", gap: 4, minWidth: 0 }}>
                            <Button
                              size="small"
                              type={active ? "primary" : "default"}
                              data-testid={`export-furnace-${furnace.id}`}
                              onClick={() => toggleFurnace(furnace.id)}
                              style={{ flex: 1, minWidth: 0 }}
                            >
                              {furnace.furnaceNo}
                            </Button>
                            {furnaceLocked ? (
                              <Popconfirm
                                title={`解锁 ${furnace.furnaceNo}？`}
                                description="解锁后该工作点本月导出数据可被改写。"
                                okText="解锁"
                                cancelText="取消"
                                disabled={!canManageMonthLock || !!lockLoadingKey}
                                onConfirm={() => void toggleMonthLocks([furnace.id], false, loadingKey)}
                              >
                                <Tooltip title="已封账，点击解锁本工作点">
                                  <Button
                                    size="small"
                                    icon={<LockFilled style={SOFT_LOCK_ICON_STYLE} />}
                                    aria-label={`解锁${furnace.furnaceNo}`}
                                    data-testid={`export-furnace-lock-${furnace.id}`}
                                    disabled={!canManageMonthLock || !!lockLoadingKey}
                                    loading={lockLoadingKey === loadingKey}
                                    style={{
                                      width: 32,
                                      paddingInline: 0,
                                      color: "#956f6b",
                                      background: "#fcf7f6",
                                      borderColor: "#eadbd8",
                                    }}
                                  />
                                </Tooltip>
                              </Popconfirm>
                            ) : (
                              <Tooltip title="点击锁定本工作点">
                                <Button
                                  size="small"
                                  icon={<UnlockOutlined style={UNLOCKED_ICON_STYLE} />}
                                  aria-label={`锁定${furnace.furnaceNo}`}
                                  data-testid={`export-furnace-lock-${furnace.id}`}
                                  disabled={!canManageMonthLock || !!lockLoadingKey}
                                  loading={lockLoadingKey === loadingKey}
                                  onClick={() => void toggleMonthLocks([furnace.id], true, loadingKey)}
                                  style={{
                                    width: 32,
                                    paddingInline: 0,
                                    color: "#6f927d",
                                    background: "#f6faf7",
                                    borderColor: "#d7e4da",
                                  }}
                                />
                              </Tooltip>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </Space>
      </Card>

      <Card title="合并导出预览">
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          {toolbar}
          {previewError && (
            <Alert
              type={previewError.status === 429 ? "info" : previewError.status === 422 ? "warning" : "error"}
              showIcon
              data-testid="export-preview-error"
              message={previewError.status === 422
                ? "预览范围过大"
                : previewError.status === 429
                  ? "报表计算繁忙"
                  : "预览失败"}
              description={previewError.status === 422
                ? `${previewError.detail} 请减少已选工作点后重试。`
                : previewError.status === 429
                  ? previewError.detail
                  : previewError.detail}
            />
          )}
          {refreshInterrupted && (
            <Alert
              type="warning"
              showIcon
              data-testid="export-refresh-interrupted"
              message="预览刷新已暂停，后续工作表尚未更新"
              description="请根据上方错误提示缩小范围或稍后重试，再点击「刷新全部预览」。已显示的旧数据可能尚未更新。"
            />
          )}
          <Alert
            type="info"
            showIcon
            data-testid="preview-filter-note"
            message="下方各表可按「工号/姓名」筛选（模糊匹配，需点击「筛选」按钮生效），仅影响本页展示；单表「导出」与「导出合并 Excel」始终导出完整数据，不受筛选影响。"
          />
          {exportWarnings.map((warning) => (
            <Alert
              key={`${warning.type}:${warning.employeeIds?.join(",") ?? warning.message}`}
              type="warning"
              showIcon
              message={warning.message}
              description={warning.employeeIds?.length
                ? `涉及工号：${warning.employeeIds.join("、")}`
                : undefined}
            />
          ))}
          <Space size="large" wrap>
            <Statistic title="出勤员工" value={payrollSheet?.rowCount ?? 0} suffix="人" />
            <Statistic title="工作点数" value={furnaceCount || selected.length} suffix="台" />
            <Statistic title="班次有效工时" value={monthlyHours?.rows.length ?? 0} suffix="行" />
            <Statistic title="加班明细" value={overtimeDetail?.rows.length ?? 0} suffix="条" />
          </Space>

          <PreviewSection
            title="输出给薪资"
            totalCount={payrollSheet?.rowCount ?? 0}
            visibleCount={payrollRowsShown.length}
            unit="人"
            filterActive={Boolean(kw.applied.payroll)}
            actions={renderPreviewActions("payroll", refreshPayroll)}
            filterControls={renderFilterControls("payroll")}
          >
            <Table<PayrollRow>
              rowKey={payrollRowKey}
              size="small"
              loading={sectionLoading.payroll}
              columns={payrollColumns}
              dataSource={payrollRowsShown}
              scroll={{ x: "max-content" }}
              pagination={tablePagination("payroll", "人")}
              locale={{ emptyText: emptyText("所选工作点当月无出勤员工") }}
            />
          </PreviewSection>

          <PreviewSection
            title="输出给薪资2"
            totalCount={payrollSheet2?.rowCount ?? 0}
            visibleCount={payroll2RowsShown.length}
            unit="行"
            filterActive={Boolean(kw.applied.payroll2)}
            actions={renderPreviewActions("payroll2", () => void refreshPayroll2())}
            filterControls={renderFilterControls("payroll2")}
          >
            <div style={{ marginBottom: 12, padding: "8px 10px", background: "#fafafa", border: "1px solid #eee", borderRadius: 6 }}>
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Space size="middle" wrap>
                  <span style={{ color: "#666" }}>合并维度</span>
                  <Checkbox.Group
                    value={payroll2GroupBy}
                    options={PAYROLL2_GROUP_FIELDS.map((field) => ({
                      label: field,
                      value: field,
                      disabled: field === "工号",
                    }))}
                    onChange={(values) => updatePayroll2GroupBy(values.map(String))}
                  />
                  <span style={{ color: "#999", fontSize: 12 }}>
                    未勾选“岗位”时，多岗位会展开为“岗位1/出勤天数1”
                  </span>
                </Space>
                <Space size="middle" wrap>
                  <Checkbox
                    checked={payroll2HideZeroRows}
                    onChange={(event) => updatePayroll2HideZeroRows(event.target.checked)}
                  >
                    隐藏无出勤、加班、请假和旷工的行
                  </Checkbox>
                  <Space size={4}>
                    <Checkbox
                      checked={payroll2MergeOvertimeOnlyRows}
                      onChange={(event) => updatePayroll2MergeOvertimeOnlyRows(event.target.checked)}
                    >
                      合并零出勤加班行
                    </Checkbox>
                    <Tooltip title={PAYROLL2_MERGE_TOOLTIP}>
                      <ExclamationCircleOutlined
                        aria-label="查看加班行合并规则"
                        style={{ color: "#faad14", fontSize: 13, cursor: "help" }}
                      />
                    </Tooltip>
                  </Space>
                  <Space size={4}>
                    <Checkbox
                      checked={payroll2MergeSameSquadOvertimeOnlyRows}
                      onChange={(event) => updatePayroll2MergeSameSquadOvertimeOnlyRows(event.target.checked)}
                    >
                      同班组合并零出勤加班行
                    </Checkbox>
                    <Tooltip title={PAYROLL2_MERGE_SAME_SQUAD_TOOLTIP}>
                      <ExclamationCircleOutlined
                        aria-label="查看同班组加班行合并规则"
                        style={{ color: "#faad14", fontSize: 13, cursor: "help" }}
                      />
                    </Tooltip>
                  </Space>
                </Space>
              </Space>
            </div>
            <Table<PayrollRow>
              rowKey={(r, i) => `${String(r["工号"] ?? "")}-${String(r["部门"] ?? "")}-${String(r["用工类型"] ?? "")}-${i}`}
              size="small"
              loading={sectionLoading.payroll2}
              columns={payroll2Columns}
              dataSource={payroll2RowsShown}
              scroll={{ x: "max-content" }}
              pagination={tablePagination("payroll2", "行")}
              locale={{ emptyText: emptyText("所选工作点当月无薪资2数据") }}
            />
          </PreviewSection>

          <PreviewSection
            title="打卡异常"
            totalCount={anomalyRows.length}
            visibleCount={anomalyRowsShown.length}
            unit="条"
            filterActive={Boolean(kw.applied.anomaly)}
            actions={renderPreviewActions("anomaly", () => refreshExtraSheet("anomaly"))}
            filterControls={renderFilterControls("anomaly")}
          >
            <Table
              rowKey={(_r, i) => String(i)}
              size="small"
              loading={sectionLoading.anomaly}
              columns={buildExtraColumns(anomalySheet?.columns ?? [])}
              dataSource={anomalyRowsShown}
              scroll={{ x: "max-content" }}
              pagination={tablePagination("anomaly", "条")}
              locale={{ emptyText: emptyText("当月无对账异常（全部自动一致）") }}
            />
          </PreviewSection>

          <PreviewSection
            title="三方对账（劳勤/工作点/补录）"
            totalCount={tripartiteRows.length}
            visibleCount={tripartiteRowsShown.length}
            unit="行"
            filterActive={Boolean(kw.applied.tripartite)}
            actions={renderPreviewActions("tripartite", () => refreshExtraSheet("tripartite"))}
            filterControls={renderFilterControls("tripartite")}
          >
            <Table
              rowKey={(_r, i) => String(i)}
              size="small"
              loading={sectionLoading.tripartite}
              columns={buildExtraColumns(tripartiteSheet?.columns ?? [])}
              dataSource={tripartiteRowsShown}
              scroll={{ x: "max-content" }}
              pagination={tablePagination("tripartite", "行")}
              locale={{ emptyText: emptyText("当月无对账数据") }}
            />
          </PreviewSection>

          <PreviewSection
            title="班次有效工时"
            totalCount={monthlyHours?.rows.length ?? 0}
            visibleCount={hoursRowsShown.length}
            unit="行"
            filterActive={Boolean(kw.applied.hours)}
            actions={renderPreviewActions("hours", refreshMonthlyHours)}
            filterControls={renderFilterControls("hours")}
          >
            <Table<GridRow>
              rowKey={(_r, i) => String(i)}
              size="small"
              loading={sectionLoading.hours}
              columns={hoursColumns}
              dataSource={hoursRowsShown}
              scroll={{ x: "max-content" }}
              pagination={tablePagination("hours", "行")}
              locale={{ emptyText: emptyText("当月无班次有效数据") }}
            />
          </PreviewSection>

          <PreviewSection
            title="加班明细"
            totalCount={overtimeDetail?.rows.length ?? 0}
            visibleCount={overtimeRowsShown.length}
            unit="条"
            filterActive={Boolean(kw.applied.overtime)}
            actions={renderPreviewActions("overtime", refreshOvertimeDetail)}
            filterControls={renderFilterControls("overtime")}
          >
            {grid("overtime", overtimeDetail?.columns, overtimeRowsShown, "当月无加班明细", "条")}
          </PreviewSection>

          <PreviewSection
            title="加班汇总"
            totalCount={overtimeDetail?.summaryRows.length ?? 0}
            unit="组"
            actions={renderPreviewActions("overtimeSummary", refreshOvertimeDetail)}
          >
            {grid(
              "overtimeSummary",
              overtimeDetail?.summaryColumns,
              overtimeDetail?.summaryRows,
              "当月无加班汇总",
              "组",
            )}
          </PreviewSection>

          <PreviewSection
            title="按人汇总"
            totalCount={overtimeDetail?.byPersonRows.length ?? 0}
            visibleCount={overtimePersonRowsShown.length}
            unit="人"
            filterActive={Boolean(kw.applied.overtimePerson)}
            actions={renderPreviewActions("overtimePerson", refreshOvertimeDetail)}
            filterControls={renderFilterControls("overtimePerson")}
          >
            {grid(
              "overtimePerson",
              overtimeDetail?.byPersonColumns,
              overtimePersonRowsShown,
              "当月无加班",
              "人",
            )}
          </PreviewSection>

          <Alert
            type="info"
            data-testid="billing-rules"
            message="计费口径（金额由下游薪资系统按此核算）"
            description={
              <div style={{ fontSize: 13, lineHeight: 1.9 }}>
                <div>
                  <b>出勤天数：</b>
                  应出勤 = 当月日历天数 - 4；实际出勤整班 8h = 1 天、半班 4h = 0.5 天，单条工时按 8h 封顶折算
                </div>
                <div>
                  <b>加班：</b>
                  出勤工时超 8h 的部分 + 跨岗位加班，按实际岗位数动态展开到「加班N/加班标准N · 加班合计」
                </div>
                <div>
                  <b>无效日：</b>
                  对账未通过 / HR·值班长判不通过的日子，不计出勤天数与加班
                </div>
              </div>
            }
          />
        </Space>
        <div style={{ color: "#999", fontSize: 12, marginTop: 8 }}>
          <Tooltip title="旷工的自动计算列始终显示系统计算值；导出优先采用已保存的手动值，未填写时采用自动值">
            口径说明
          </Tooltip>
          ：下方平铺列表即合并 Excel 的各个工作表内容。改月份或工作点后，请点击「生成全部预览」。
        </div>
      </Card>
    </Space>
  );
}
