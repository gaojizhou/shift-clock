import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, DatePicker, Input, NavBar, Popup, Selector, SpinLoading, Tabs, Toast } from "antd-mobile";
import { CalendarOutline } from "antd-mobile-icons";
import { QRCodeSVG } from "@rc-component/qrcode";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  addChiefWorker,
  addOvertime,
  getWorkerDay,
  apiGet,
  apiPost,
  buildScanPayload,
  deleteChiefWorker,
  FULL_SHIFT_HOURS,
  getChiefWorkers,
  getDemoContext,
  getFurnaces,
  getMonthlyExportLock,
  getProxyQuota,
  getSelectablePosts,
  isHalfShift,
  overtimeHoursLabel,
  searchEmployees,
  SHIFT_SLOTS,
  SQUADS,
  type ChiefWorker,
  type DemoContext,
  type EmployeeItem,
  type FurnaceItem,
  type MonthlyExportFurnaceLockStatus,
  type ProxyQuota,
} from "@fdc/api-client";
import { useAuth } from "../../auth";
import { MAX_SHIFT_EXTENSION_HOURS, MAX_WORK_HOURS, ROSTER_SQUAD_TONE, SQUADLESS } from "./constants";
import { RosterPostGroup } from "./components/RosterPostGroup";
import type { Evidence, OvertimeCandidate, Person, Sections, SquadName } from "./types";
import { attendanceHoursLabel, capHoursInput, rosterTag, roundHours, validateWorkHours } from "./utils";

function employmentStatusBadge(status: "active" | "departed") {
  const departed = status === "departed";
  return (
    <span
      style={{
        display: "inline-block",
        marginLeft: 6,
        padding: "0 5px",
        borderRadius: 4,
        fontSize: "0.78rem",
        lineHeight: 1.5,
        color: departed ? "#b42318" : "#4f7a45",
        background: departed ? "#fff1f0" : "#f2f7ef",
      }}
    >
      {departed ? "离职" : "在职"}
    </span>
  );
}

export default function ConfirmPage() {
  const { me } = useAuth();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const paramFurnace = params.get("furnace");
  const paramRegion = params.get("region");
  // 默认值仅在 URL 未指定、且会话值班长未绑定工作点时兜底；值班长登录后用其绑定工作点覆盖。
  const [furnace, setFurnace] = useState(paramFurnace || "35号工作点");
  const [region, setRegion] = useState(paramRegion || "示例厂区A");
  const [boundByChief, setBoundByChief] = useState(false);
  const [demoCtx, setDemoCtx] = useState<DemoContext | null>(null);
  const [tab, setTab] = useState("today");
  const [activeRosterSquad, setActiveRosterSquad] = useState<SquadName | "">("");
  // 任意日期查看：右侧日期选择器（除 今天/昨天/前天 外）
  const [customDate, setCustomDate] = useState("");
  const [datePickerVisible, setDatePickerVisible] = useState(false);
  const [sections, setSections] = useState<Sections | null>(null);
  const [loading, setLoading] = useState(false);
  const sessionRole = me.role || null;
  const sessionUserid = me.userid || null;
  const sessionName = me.name || null;
  const [furnaceItem, setFurnaceItem] = useState<FurnaceItem | null>(null);
  const [monthLock, setMonthLock] = useState<MonthlyExportFurnaceLockStatus | null>(null);
  const [showQR, setShowQR] = useState(false);
  const furnaceType = furnaceItem?.furnaceType ?? "";

  // 值班长-常用员工映射 + 代签选人 / 常用员工管理
  const [chiefWorkers, setChiefWorkers] = useState<ChiefWorker[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerPost, setPickerPost] = useState("");
  const [pickerSquad, setPickerSquad] = useState<string | null>(null);
  const [manageOpen, setManageOpen] = useState(false);
  const [searchKw, setSearchKw] = useState("");
  const [searchResults, setSearchResults] = useState<EmployeeItem[]>([]);
  const [proxyOpen, setProxyOpen] = useState(false);
  const [proxyEmp, setProxyEmp] = useState<string | null>(null);
  const [proxyPost, setProxyPost] = useState("出料工");
  const [proxySquad, setProxySquad] = useState("1班");
  const [proxyType, setProxyType] = useState("正式"); // 代签用工类型，默认正式
  const [proxyHalf, setProxyHalf] = useState(false); // 代签是否半班，默认整班
  const [proxyQuota, setProxyQuota] = useState<ProxyQuota | null>(null);
  const [posts, setPosts] = useState<string[]>([]);

  // 「添加工人」弹窗（代签 / 加班 两 Tab）
  const [addOpen, setAddOpen] = useState(false);
  const [addTab, setAddTab] = useState("proxy");
  // 加班：选人 + 表单
  const [otEmp, setOtEmp] = useState<OvertimeCandidate | null>(null);
  const [otSquad, setOtSquad] = useState<string | null>(null);
  const [otShiftSlot, setOtShiftSlot] = useState<string>("");
  const [otPost, setOtPost] = useState("");
  const [otType, setOtType] = useState("正式");
  const [otHours, setOtHours] = useState("8");
  const [otSubmitting, setOtSubmitting] = useState(false);
  // 加班：搜索其他操作工（须当天已有记录）
  const [otSearchKw, setOtSearchKw] = useState("");
  const [otSearchResults, setOtSearchResults] = useState<EmployeeItem[]>([]);

  // 工作点项加载失败与「未匹配到工作点」区分开：失败时保留上一次月锁门禁，
  // 但 furnaceItem 必须与当前 region/furnace 匹配，避免二维码/编制串台。
  const [furnaceItemError, setFurnaceItemError] = useState(false);
  const [lastKnownMonthLocked, setLastKnownMonthLocked] = useState(false);
  const furnaceItemMatches = furnaceItem?.region === region && furnaceItem?.furnaceNo === furnace;
  const loadFurnaceItem = useCallback(() => {
    return getFurnaces()
      .then((r) => {
        setFurnaceItemError(false);
        setFurnaceItem(r.items.find((f) => f.region === region && f.furnaceNo === furnace) ?? null);
      })
      .catch(() => setFurnaceItemError(true));
  }, [region, furnace]);

  useEffect(() => {
    setFurnaceItem(null);
    setFurnaceItemError(false);
  }, [region, furnace]);

  const loadSelectablePosts = useCallback(() => {
    return getSelectablePosts(region, furnace, "worker")
      .then((r) => {
        setPosts(r.posts);
        if (r.posts.length) setProxyPost((prev) => (r.posts.includes(prev) ? prev : r.posts[0]));
      })
      .catch(() => setPosts([]));
  }, [region, furnace]);

  useEffect(() => {
    getDemoContext().then(setDemoCtx);
    // 代签是给「工人」上岗，按 worker 角色取可选岗
    void loadSelectablePosts();
    void loadFurnaceItem();
  }, [region, furnace, loadFurnaceItem, loadSelectablePosts]);

  useEffect(() => {
    // 值班长绑定了工作点且 URL 未显式指定 → 用其绑定工作点
    if (me.furnace && !paramFurnace) {
      setFurnace(me.furnace);
      if (me.region) setRegion(me.region);
      setBoundByChief(true);
    }
  }, [me.furnace, me.region, paramFurnace]);

  const dateTabs = useMemo(() => {
    if (!demoCtx) return [];
    return [
      { key: "today", label: "今天", date: demoCtx.today },
      { key: "t1", label: "昨天", date: demoCtx.t1 },
      { key: "t2", label: "前天", date: demoCtx.t2 },
    ];
  }, [demoCtx]);

  const date =
    tab === "custom" && customDate
      ? customDate
      : dateTabs.find((d) => d.key === tab)?.date ?? demoCtx?.t2 ?? "";

  // 提交成功/失败后抬升 tick 重查月锁，避免封账状态变化后前端门禁滞后。
  const [monthLockTick, setMonthLockTick] = useState(0);
  useEffect(() => {
    const month = date.slice(0, 7);
    const furnaceId = furnaceItemMatches ? furnaceItem?.id : undefined;
    if (!/^\d{4}-\d{2}$/.test(month) || (furnaceId == null && !furnaceItemError)) {
      if (!furnaceItemError) setMonthLock(null);
      return;
    }
    if (furnaceId == null) {
      // 工作点项加载失败：保留上一次月锁状态，保守不放开写操作门禁。
      return;
    }
    let alive = true;
    getMonthlyExportLock(month, furnaceId)
      .then((s) => {
        if (alive) {
          const next = s.items[0] ?? null;
          setMonthLock(next);
          setLastKnownMonthLocked(!!next?.locked);
        }
      })
      .catch(() => {
        if (alive) setMonthLock(null);
      });
    return () => {
      alive = false;
    };
  }, [date, furnaceItem?.id, furnaceItemError, furnaceItemMatches, monthLockTick]);

  const writeLocked = furnaceItemMatches ? !!monthLock?.locked : lastKnownMonthLocked;

  const load = useCallback(() => {
    if (!date) return Promise.resolve();
    setLoading(true);
    return Promise.all([
      apiGet<Sections>(
        `/confirm?furnace=${encodeURIComponent(furnace)}&region=${encodeURIComponent(region)}&date=${date}`
      ),
      loadFurnaceItem(),
      loadSelectablePosts(),
    ])
      .then(([nextSections]) => setSections(nextSections))
      .finally(() => setLoading(false));
  }, [date, furnace, region, loadFurnaceItem, loadSelectablePosts]);

  useEffect(() => {
    if (sessionRole === "chief" || sessionRole === "admin") {
      void load();
    }
  }, [load, sessionRole]);

  // 当前编制：参考管理后台模板展示——不分班在上、分班（早/中/晚班）在下；
  // 每岗位先按编制铺正式岗空位，正式岗扫码的人填入空位；实习/待岗扫了的人附在其下。
  const roster = useMemo(() => {
    const rules = furnaceItem?.quotaRules ?? [];
    const scanned = (sections?.scanned ?? []).filter((s) => !s.excluded);
    // 加班记录也占编制，并入编制视图（标 overtime，前端加「加班」标签）
    const overtime: Person[] = (sections?.overtime ?? []).map((o) => ({
      empId: o.empId,
      name: o.name,
      post: o.post,
      squad: o.squad,
      employType: o.employType,
      workHours: o.workHours,
      overtime: true,
      overtimeId: o.id,
      boundToChief: true, // 加班由值班长添加，不判「外来」
      boundToAnyChief: true,
    }));
    const all = [...scanned, ...overtime];
    const group = (squad: string | null, post: string) => {
      const here = all.filter((s) => (s.squad ?? null) === squad && s.post === post);
      return {
        post,
        formal: here.filter((s) => s.employType === "正式"),
        extras: here.filter((s) => s.employType !== "正式"),
      };
    };
    const squadless = rules
      .filter((r) => r.squadless)
      .map((r) => ({ ...group(null, r.post), capacity: r.capacity }));
    const squadBlocks = SQUADS
      .map((sq) => ({
        squad: sq,
        posts: rules
          .filter((r) => !r.squadless && r.squad === sq)
          .map((r) => ({ ...group(sq, r.post), capacity: r.capacity })),
      }))
      .filter((b) => b.posts.length > 0);
    return { squadless, squadBlocks, hasRules: rules.length > 0 };
  }, [furnaceItem, sections]);

  useEffect(() => {
    const squads = roster.squadBlocks.map((b) => b.squad);
    if (!squads.length) {
      if (activeRosterSquad) setActiveRosterSquad("");
      return;
    }
    if (!activeRosterSquad || !squads.includes(activeRosterSquad)) {
      setActiveRosterSquad(squads[0]);
    }
  }, [activeRosterSquad, roster.squadBlocks]);

  const selectedRosterSquad = activeRosterSquad || roster.squadBlocks[0]?.squad || "";
  const activeRosterBlock = roster.squadBlocks.find((b) => b.squad === selectedRosterSquad);
  const activeRosterTone = ROSTER_SQUAD_TONE[selectedRosterSquad] ?? ROSTER_SQUAD_TONE["1班"];

  // 代签弹窗：班组与岗位都来自本工作点编制规则（含不分班岗，如维修工/值班长），
  // 而非按 worker 角色过滤的可选岗；岗位随所选班组联动致灰，正编满禁止「正式」代签。
  const proxyRules = useMemo(() => furnaceItem?.quotaRules ?? [], [furnaceItem]);

  // 班组选项：有不分班岗则提供「不分班」，分班沿用配置过的早/中/晚班。
  const proxySquadOptions = useMemo(() => {
    const opts: { label: string; value: string }[] = [];
    if (proxyRules.some((r) => r.squadless)) opts.push({ label: SQUADLESS, value: SQUADLESS });
    for (const sq of SQUADS) {
      if (proxyRules.some((r) => !r.squadless && r.squad === sq)) opts.push({ label: sq, value: sq });
    }
    return opts;
  }, [proxyRules]);

  // 全部岗位（按规则顺序去重，不分班岗在前）。
  const proxyAllPosts = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const r of proxyRules)
      if (!seen.has(r.post)) {
        seen.add(r.post);
        out.push(r.post);
      }
    return out;
  }, [proxyRules]);

  // 当前所选班组下「有编制」的岗位；不在其中的岗位在选择器里置灰。
  const proxyValidPosts = useMemo(() => {
    const set = new Set<string>();
    for (const r of proxyRules) {
      const ok = proxySquad === SQUADLESS ? r.squadless : !r.squadless && r.squad === proxySquad;
      if (ok) set.add(r.post);
    }
    return set;
  }, [proxyRules, proxySquad]);

  // 当前所选岗位+班组的正编占用：累加该组合下「正式」有效打卡的工时（半班=4h），按 /8 还原成人数。
  const proxyCapacity = useMemo(() => {
    const wantSquad = proxySquad === SQUADLESS ? null : proxySquad;
    const rule = proxyRules.find(
      (r) =>
        r.post === proxyPost &&
        (proxySquad === SQUADLESS ? r.squadless : !r.squadless && r.squad === proxySquad)
    );
    if (!rule) return null;
    const used = (sections?.scanned ?? [])
      .filter(
        (s) =>
          !s.excluded &&
          s.employType === "正式" &&
          s.post === proxyPost &&
          (s.squad ?? null) === wantSquad
      )
      .reduce((sum, s) => sum + (s.workHours ?? 8) / 8, 0);
    return { capacity: rule.capacity, used, full: used >= rule.capacity };
  }, [proxyRules, proxyPost, proxySquad, sections]);

  useEffect(() => {
    if (sessionRole === "chief" || sessionRole === "admin") {
      getChiefWorkers()
        .then((r) => setChiefWorkers(r.items))
        .catch(() => undefined);
    }
  }, [sessionRole]);

  const doSearch = () => {
    const kw = searchKw.trim();
    if (!kw) {
      setSearchResults([]);
      return;
    }
    searchEmployees(kw)
      .then((r) => setSearchResults(r.items))
      .catch(() => setSearchResults([]));
  };

  const openPicker = (postName: string, squadName: string | null) => {
    setPickerPost(postName);
    setPickerSquad(squadName);
    setSearchKw("");
    setSearchResults([]);
    setPickerOpen(true);
  };

  const overtimeDefaultsFromPicker = (postName = pickerPost, squadName = pickerSquad) => {
    const ok =
      !!squadName &&
      proxyRules.some((r) => !r.squadless && r.squad === squadName && r.post === postName);
    return ok ? { squad: squadName, post: postName } : { squad: null, post: "" };
  };

  // 每个岗位空位右侧的「添加工人」：打开 代签/加班 两 Tab 弹窗，并按该岗位/班次定标
  const openAddWorker = (postName: string, squadName: string | null) => {
    const defaults = overtimeDefaultsFromPicker(postName, squadName);
    setPickerPost(postName);
    setPickerSquad(squadName);
    setSearchKw("");
    setSearchResults([]);
    setOtEmp(null);
    setOtSquad(defaults.squad);
    setOtShiftSlot("");
    setOtPost(defaults.post);
    setOtHours(String(FULL_SHIFT_HOURS));
    setAddTab("proxy");
    setAddOpen(true);
  };

  const switchAddTab = (key: string) => {
    setAddTab(key);
    if (key === "overtime") {
      const defaults = overtimeDefaultsFromPicker();
      setOtEmp(null);
      setOtSquad(defaults.squad);
      setOtShiftSlot("");
      setOtPost(defaults.post);
      setOtHours(String(FULL_SHIFT_HOURS));
      setOtSearchKw("");
      setOtSearchResults([]);
    }
  };

  const bindWorker = async (empId: string) => {
    const r = await addChiefWorker(empId);
    setChiefWorkers(r.items);
    Toast.show({ content: "已绑定为常用员工" });
    load();
  };

  const removeWorker = async (empId: string) => {
    if (!sessionUserid) return;
    const r = await deleteChiefWorker(sessionUserid, empId);
    setChiefWorkers(r.items);
    load();
  };

  const submit = async (lock = true) => {
    try {
      const res = await apiPost<{ locked: boolean; confirmedBy: string }>("/confirm/submit", {
        furnace,
        region,
        date,
        locked: lock,
      });
      Toast.show({ content: lock ? `已锁定 · ${res.confirmedBy}` : "已解锁，可继续修改" });
      load();
    } catch (e) {
      // 后端守卫（如月度导出锁 409）必须可见，不能静默吞掉让值班长误以为已提交。
      const err = e as { status?: number; data?: { detail?: string } };
      Toast.show({ content: err.data?.detail || (err.status === 409 ? "该日已封账或已锁定，无法提交" : "提交失败，请重试") });
      load();
    } finally {
      setMonthLockTick((tick) => tick + 1);
    }
  };

  const [cancelEmp, setCancelEmp] = useState<string | null>(null);
  const [cancelReason, setCancelReason] = useState("错扫");
  const [detailEmp, setDetailEmp] = useState<Person | null>(null);
  const [detailEvidence, setDetailEvidence] = useState<Evidence | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [reassignSquad, setReassignSquad] = useState<string>("1班");
  // 值班长在「打卡证据」弹窗里的操作改为「先选后提交」：暂存一项待提交动作，点「提交」才落库
  type PendingAction =
    | { kind: "fraction"; workHours: number; recordId?: number; label: string }
    | { kind: "exclude"; reason: string; label: string }
    | { kind: "restore"; label: string }
    | { kind: "reassign"; post: string; squad: string; label: string };
  const [pending, setPending] = useState<PendingAction | null>(null);
  // 详情弹窗里的「修改工作时长」数字框。
  const [detailHours, setDetailHours] = useState("8");

  const openDetail = (person: Person) => {
    setDetailEmp(person);
    setReassignSquad(person.squad ?? "1班");
    setDetailHours(String(person.workHours ?? 8));
    setPending(null);
    setDetailEvidence(null);
    setDetailLoading(true);
    void apiGet<Evidence>(
      `/anomalies/evidence?empId=${encodeURIComponent(person.empId)}&date=${date}`
    )
      .then(setDetailEvidence)
      .catch(() => Toast.show({ content: "加载证据失败" }))
      .finally(() => setDetailLoading(false));
  };

  const excludeScan = async (empId: string, excluded: boolean, reason?: string) => {
    try {
      await apiPost("/confirm/exclude", {
        furnace,
        region,
        date,
        empId,
        excluded,
        reason: excluded ? reason : undefined,
      });
      Toast.show({ content: excluded ? "已撤回打卡" : "已恢复打卡" });
      load();
    } catch (e) {
      const err = e as { status?: number; data?: { detail?: string } };
      const msg =
        err.status === 409
          ? err.data?.detail || "当天已有可用打卡，撤回记录不可恢复"
          : err.data?.detail || "操作失败";
      Toast.show({ content: msg });
      // 守卫失败/状态变化后关闭详情并刷新，避免反复点击无反应
      setDetailEmp(null);
      load();
    }
  };

  const setHours = async (
    empId: string,
    workHours: number,
    recordId?: number,
  ) => {
    try {
      await apiPost("/confirm/hours", {
        furnace,
        region,
        date,
        empId,
        workHours,
        ...(recordId !== undefined ? { recordId } : {}),
      });
      Toast.show({
        content: workHours === 0 ? "已取消该记录" : `已更新工时为 ${workHours}h`,
      });
      load();
    } catch (e) {
      const err = e as { status?: number; data?: { detail?: string } };
      Toast.show({ content: err.data?.detail || (err.status === 409 ? "该日已封账或已锁定，无法修改" : "操作失败") });
      setDetailEmp(null);
      load();
    }
  };

  // 详情弹窗：修改时长即时暂存（无需单独「暂存」按钮）；0 小时=撤回打卡
  const stageHours = (rawHours: string) => {
    const wh = Number(rawHours);
    if (validateWorkHours(wh)) {
      setPending(null);
      return;
    }
    const isOt = !!detailEmp?.overtime;
    setPending({
      kind: "fraction",
      workHours: wh,
      recordId: isOt ? detailEmp?.overtimeId : undefined,
      label: wh === 0 ? (isOt ? "取消加班" : "撤回打卡") : `改为 ${overtimeHoursLabel(wh)}`,
    });
  };

  // 延班时长 ↔ 工作时长 联动：工作时长 = 8(整班) + 延班；总时长不超过 24h。
  const applyOt = (rawOt: number) => {
    const ot = Math.min(MAX_SHIFT_EXTENSION_HOURS, Math.max(0, Number.isFinite(rawOt) ? rawOt : 0));
    const wh = String(FULL_SHIFT_HOURS + ot);
    setDetailHours(wh);
    stageHours(wh);
  };

  // 加班候选：当天已有 active 记录的人（squads=已占班次，用于置灰）
  const overtimeCandidates = sections?.overtimeCandidates ?? [];

  // 加班可选班组：本工作点分班编制里的所有班次（不分班岗不在此处理）。
  // 同一班次可上不同岗位 → 班次不再因「已占」置灰；重复的「班次+岗位」由后端拦。
  const otSquadOptions = useMemo(() => {
    const opts: { label: string; value: string }[] = [];
    for (const sq of SQUADS) {
      if (proxyRules.some((r) => !r.squadless && r.squad === sq)) {
        opts.push({ label: sq, value: sq });
      }
    }
    return opts;
  }, [proxyRules]);

  // 加班岗位：当前所选班组下有编制的分班岗
  const otValidPosts = useMemo(() => {
    if (!otSquad) return [] as string[];
    const seen = new Set<string>();
    const out: string[] = [];
    for (const r of proxyRules) {
      if (!r.squadless && r.squad === otSquad && !seen.has(r.post)) {
        seen.add(r.post);
        out.push(r.post);
      }
    }
    return out;
  }, [proxyRules, otSquad]);

  // 加班次正编占用：编制含「出勤+加班」的正式工(工时按8h封顶折算编制位)，与后端越编硬拦口径一致
  const otCapacity = useMemo(() => {
    if (!otSquad || !otPost) return null;
    const rule = proxyRules.find((r) => r.post === otPost && !r.squadless && r.squad === otSquad);
    if (!rule) return null;
    const usedScanned = (sections?.scanned ?? [])
      .filter((s) => !s.excluded && s.employType === "正式" && s.post === otPost && (s.squad ?? null) === otSquad)
      .reduce((sum, s) => sum + Math.min(s.workHours ?? 8, 8) / 8, 0);
    const usedOt = (sections?.overtime ?? [])
      .filter((o) => o.employType === "正式" && o.post === otPost && (o.squad ?? null) === otSquad)
      .reduce((sum, o) => sum + Math.min(o.workHours ?? 8, 8) / 8, 0);
    const used = Math.round((usedScanned + usedOt) * 100) / 100;
    return { capacity: rule.capacity, used, full: used >= rule.capacity };
  }, [proxyRules, otPost, otSquad, sections]);
  const otFormalFull = otType === "正式" && (otCapacity?.full ?? false);
  const otRemainingHours = Math.max(0, Math.min(MAX_WORK_HOURS, otEmp?.remainingHours ?? MAX_WORK_HOURS));

  const openOvertime = (cand: OvertimeCandidate) => {
    const remaining = Math.max(0, Math.min(MAX_WORK_HOURS, cand.remainingHours ?? MAX_WORK_HOURS));
    const defaults = overtimeDefaultsFromPicker();
    setOtEmp(cand);
    setOtSquad(cand.baseSquad ?? defaults.squad);
    setOtShiftSlot("");
    setOtPost(defaults.post);
    setOtType("正式");
    setOtHours(String(Math.min(FULL_SHIFT_HOURS, remaining)));
  };

  const doOtSearch = async () => {
    const kw = otSearchKw.trim();
    if (!kw) return;
    try {
      const r = await searchEmployees(kw);
      setOtSearchResults(r.items);
    } catch {
      setOtSearchResults([]);
    }
  };

  // 搜到的工人选入加班：先查其当天是否已有记录（加班须基于已有出勤），并带出已占班次
  const pickOvertimeSearched = async (emp: EmployeeItem) => {
    try {
      const wd = await getWorkerDay(emp.empId, date);
      if (!wd.hasRecord) {
        Toast.show({ content: "该操作工当天无记录，请改用「代签」" });
        return;
      }
      openOvertime({
        empId: emp.empId,
        name: emp.name,
        employmentStatus: emp.employmentStatus,
        squads: wd.squads,
        baseSquad: wd.baseSquad,
        slots: wd.slots,
        usedHours: wd.usedHours,
        remainingHours: wd.remainingHours,
      });
    } catch {
      Toast.show({ content: "查询失败" });
    }
  };

  const submitOvertime = async () => {
    if (!otEmp || !otSquad || !otPost) return;
    const wh = Number(otHours);
    const remaining = Math.max(0, Math.min(MAX_WORK_HOURS, otEmp.remainingHours ?? MAX_WORK_HOURS));
    if (!Number.isFinite(wh) || wh <= 0) {
      Toast.show({ content: "请输入有效时长" });
      return;
    }
    if (!otShiftSlot) {
      Toast.show({ content: "请选择加班时间段" });
      return;
    }
    if (wh > remaining) {
      Toast.show({ content: `该员工当天最多还可加班 ${remaining}h` });
      return;
    }
    setOtSubmitting(true);
    try {
      const res = await addOvertime({
        empId: otEmp.empId,
        date,
        furnace,
        region,
        squad: otSquad,
        shiftSlot: otShiftSlot,
        post: otPost,
        employType: otType,
        workHours: wh,
      });
      Toast.show({ content: `已添加加班 · ${overtimeHoursLabel(res.workHours)}` });
      setOtEmp(null);
      setAddOpen(false);
      load();
    } catch (e) {
      const detail = (e as { data?: { detail?: string } }).data?.detail;
      Toast.show({ content: detail || "加班添加失败" });
    } finally {
      setOtSubmitting(false);
    }
  };

  const reassignScan = async (empId: string, toPost: string, toSquad: string | null) => {
    try {
      await apiPost("/confirm/reassign", { furnace, region, date, empId, post: toPost, squad: toSquad });
      Toast.show({ content: `已调整为 ${toSquad ?? ""} ${toPost}`.trim() });
      setDetailEmp(null);
      load();
    } catch {
      Toast.show({ content: "调整失败" });
    }
  };

  // 提交暂存的值班长操作：执行对应接口后清空暂存并关闭弹窗
  const submitPending = async () => {
    if (!detailEmp || !pending) return;
    const empId = detailEmp.empId;
    if (pending.kind === "fraction") {
      const err = validateWorkHours(pending.workHours);
      if (err) {
        Toast.show({ content: err });
        return;
      }
      await setHours(empId, pending.workHours, pending.recordId);
    } else if (pending.kind === "exclude") await excludeScan(empId, true, pending.reason);
    else if (pending.kind === "restore") await excludeScan(empId, false);
    else await reassignScan(empId, pending.post, pending.squad);
    setPending(null);
    setDetailEmp(null);
  };

  const confirmCancel = async () => {
    if (!cancelEmp || !cancelReason.trim()) {
      Toast.show({ content: "请填写取消原因" });
      return;
    }
    await excludeScan(cancelEmp, true, cancelReason);
    setCancelEmp(null);
  };

  const openProxy = (empId: string, post?: string, squad?: string | null) => {
    setProxyEmp(empId);
    setProxyQuota(null);
    setProxyType("正式");
    setProxyHalf(false);
    if (post) setProxyPost(post);
    if (squad !== undefined) setProxySquad(squad ?? SQUADLESS);
    setProxyOpen(true);
    if (date) {
      void getProxyQuota(empId, date)
        .then(setProxyQuota)
        .catch(() => setProxyQuota(null));
    }
  };

  const proxyScan = async () => {
    if (!proxyEmp || proxyBlocked) return;
    try {
      const res = await apiPost<{ proxyQuota?: ProxyQuota }>("/makeup/approve", {
        empId: proxyEmp,
        date,
        furnace,
        region,
        post: proxyPost,
        squad: proxySquad === SQUADLESS ? null : proxySquad,
        employType: proxyType,
        workHours: proxyHalf ? 4 : 8,
      });
      const q = res.proxyQuota;
      Toast.show({ content: q ? `已代签到 · 本月 ${q.used}/${q.limit}` : "已代签到" });
      setProxyOpen(false);
      setProxyEmp(null);
      load();
    } catch (e) {
      const data = (e as { data?: { detail?: { code?: string; message?: string } | string } }).data;
      const detail = data?.detail;
      if (typeof detail === "object" && detail?.code === "PROXY_LIMIT_EXCEEDED") {
        Toast.show({
          content: detail.message || "本月代签已达上限，请联系人事/统计临时加量",
        });
        if (date && proxyEmp) void getProxyQuota(proxyEmp, date).then(setProxyQuota);
      } else {
        Toast.show({ content: typeof detail === "string" ? detail : "代签失败" });
      }
    }
  };

  const proxyExhausted = proxyQuota != null && proxyQuota.remaining <= 0;
  // 所选岗位是否在当前班组有编制（联动校验）
  const proxyComboValid = proxyValidPosts.has(proxyPost);
  // 正编已满且按「正式」代签 → 禁止（实习/待岗不占正编，仍可代签）
  const proxyFormalFull = proxyType === "正式" && (proxyCapacity?.full ?? false);
  const proxyBlocked = writeLocked || proxyExhausted || !proxyComboValid || proxyFormalFull;

  const locked = sections?.locked ?? false;
  const isChief = sessionRole === "chief" || sessionRole === "admin";

  return (
    <div style={{ background: "#f5f5f5", minHeight: "100vh", paddingBottom: 80 }}>
      <NavBar onBack={() => nav(-1)} style={{ background: "#1677FF", color: "#fff" }}>
        值班长日确认
      </NavBar>
      {isChief && loading && !sections && (
        <div
          data-testid="confirm-loading"
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 10,
            padding: "56px 0",
            color: "#999",
          }}
        >
          <SpinLoading style={{ "--size": "32px" }} />
          <span style={{ fontSize: 13 }}>加载确认数据…</span>
        </div>
      )}
      {!isChief && (
        <div
          style={{
            margin: 12,
            padding: 16,
            background: "#fff7e6",
            borderRadius: 8,
            border: "1px solid #ffd591",
          }}
          data-testid="confirm-non-chief-guide"
        >
          <div style={{ fontWeight: 600, marginBottom: 8 }}>需以值班长身份进入</div>
          <p style={{ fontSize: "1rem", color: "#666", margin: 0 }}>
            值班长日确认需以值班长身份进入。当前会话：
            {sessionUserid ? `${sessionUserid}（${sessionRole ?? "未知角色"}）` : "未登录"}
            。请在首页选择「值班长 · 张师傅」persona 后再访问本页。
          </p>
        </div>
      )}
      {isChief && (
        <>
          <div
            data-testid="chief-bound-furnace"
            style={{
              position: "sticky",
              top: 0,
              zIndex: 100,
              margin: "0 12px 12px",
              padding: "10px 14px",
              background: "#fff",
              borderRadius: 8,
              borderLeft: "4px solid #1677FF",
              boxShadow: "0 2px 8px rgba(0,0,0,0.08)",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "flex-start",
            }}
          >
            <div>
              <span style={{ fontSize: "0.92rem", color: "#999" }}>我负责的工作点</span>
              <div style={{ fontSize: "1.1rem", fontWeight: 600, marginTop: 2 }}>
                {region} · {furnace}
                {!boundByChief && (
                  <span style={{ fontSize: "0.85rem", color: "#fa8c16", marginLeft: 8 }}>（未绑定，默认）</span>
                )}
              </div>
              {furnaceType && (
                <div style={{ fontSize: "0.92rem", color: "#888", marginTop: 2 }}>功率：{furnaceType}</div>
              )}
              <div style={{ fontSize: "0.92rem", color: "#666", marginTop: 2 }}>
                本工作点值班长：
                {(sections?.chiefs && sections.chiefs.length > 0
                  ? sections.chiefs
                  : [{ empId: sessionUserid || "-", name: sessionName }]
                ).map((c, i) => (
                  <span key={c.empId}>
                    {i > 0 ? "、" : ""}
                    {c.name || c.empId}
                    {c.empId === sessionUserid ? "(我)" : ""}
                  </span>
                ))}
              </div>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, flexShrink: 0 }}>
              <Button
                size="small"
                color="primary"
                fill="outline"
                data-testid="chief-furnace-qr"
                style={{ whiteSpace: "nowrap" }}
                onClick={() => setShowQR(true)}
              >
                二维码
              </Button>
              <Button
                size="small"
                fill="outline"
                data-testid="manage-chief-workers"
                style={{ whiteSpace: "nowrap" }}
                onClick={() => {
                  setSearchKw("");
                  setSearchResults([]);
                  setManageOpen(true);
                }}
              >
                管理常用员工
              </Button>
            </div>
          </div>
          <Popup visible={showQR} onMaskClick={() => setShowQR(false)} bodyStyle={{ padding: 24, textAlign: "center" }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
              {region} · {furnace}
            </div>
            {furnaceType && <div style={{ fontSize: "0.92rem", color: "#888", marginBottom: 12 }}>功率：{furnaceType}</div>}
            <div style={{ display: "inline-block", padding: 12, background: "#fff", border: "1px solid #eee", borderRadius: 8 }}>
              {furnaceItemMatches && furnaceItem?.code ? (
                <QRCodeSVG value={buildScanPayload(furnaceItem.code)} size={200} />
              ) : (
                <div data-testid="confirm-qr-unavailable">工作点信息未就绪，请重试后再出示二维码</div>
              )}
            </div>
            <div style={{ color: "#999", fontSize: "0.92rem", marginTop: 12 }}>
              用钉钉「扫一扫」扫此码即可进入该工作点上岗页
            </div>
          </Popup>

          {writeLocked && (
            <div
              data-testid="confirm-month-export-locked"
              style={{
                margin: "0 12px 12px",
                padding: "10px 14px",
                background: "#fff1f0",
                border: "1px solid #ffa39e",
                borderRadius: 8,
                color: "#a8071a",
                lineHeight: 1.6,
              }}
            >
              <div style={{ fontWeight: 700, fontSize: "1rem" }}>
                {monthLock?.month ?? date.slice(0, 7)} 月度导出已锁定
              </div>
              <div style={{ fontSize: "0.92rem" }}>
                当前工作点在该月份的补录、代签、加班、修改工时、值班长确认等会影响导出数据的操作会被系统拦截；如需修改请联系 HR/Admin 解锁。
              </div>
            </div>
          )}

          {/* 代签选人：常用员工 + 搜索 */}
          <Popup visible={pickerOpen} onMaskClick={() => setPickerOpen(false)} bodyStyle={{ padding: 16, maxHeight: "72vh", overflow: "auto" }}>
            <h4 style={{ marginTop: 0 }}>
              代签选人 · {pickerPost}
              {pickerSquad ? ` · ${pickerSquad}` : ""}
            </h4>
            <div style={{ fontSize: "0.92rem", color: "#999", margin: "8px 0 4px" }}>常用员工</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {chiefWorkers.map((w) => (
                <Button
                  key={w.empId}
                  size="small"
                  onClick={() => {
                    setPickerOpen(false);
                    openProxy(w.empId, pickerPost, pickerSquad);
                  }}
                >
                  {w.name ?? w.empId}（{w.empId}）
                  {employmentStatusBadge(w.employmentStatus)}
                </Button>
              ))}
              {chiefWorkers.length === 0 && <span style={{ color: "#bbb", fontSize: "1rem" }}>暂无常用员工</span>}
            </div>
            <div style={{ fontSize: "0.92rem", color: "#999", margin: "12px 0 4px" }}>搜索其他员工</div>
            <div style={{ display: "flex", gap: 8 }}>
              <Input placeholder="工号或姓名" value={searchKw} onChange={setSearchKw} onEnterPress={doSearch} />
              <Button size="small" onClick={doSearch}>搜索</Button>
            </div>
            <div style={{ marginTop: 8 }}>
              {searchResults.map((e) => (
                <Button
                  key={e.empId}
                  block
                  style={{ marginBottom: 6, textAlign: "left" }}
                  onClick={() => {
                    setPickerOpen(false);
                    openProxy(e.empId, pickerPost, pickerSquad);
                  }}
                >
                  {e.name ?? e.empId}（{e.empId}）{e.role === "chief" ? " · 值班长" : ""}
                  {employmentStatusBadge(e.employmentStatus)}
                </Button>
              ))}
            </div>
          </Popup>

          {/* 管理常用员工：增删 */}
          <Popup visible={manageOpen} onMaskClick={() => setManageOpen(false)} bodyStyle={{ padding: 16, maxHeight: "72vh", overflow: "auto" }}>
            <h4 style={{ marginTop: 0 }}>常用员工（代签名单）</h4>
            {chiefWorkers.map((w) => (
              <div key={w.empId} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", borderBottom: "1px solid #f5f5f5" }}>
                <span>
                  {w.name ?? w.empId}
                  <span style={{ color: "#bbb", marginLeft: 6 }}>{w.empId}</span>
                  {employmentStatusBadge(w.employmentStatus)}
                </span>
                <Button size="mini" color="danger" fill="outline" onClick={() => void removeWorker(w.empId)}>
                  删除
                </Button>
              </div>
            ))}
            {chiefWorkers.length === 0 && <div style={{ color: "#bbb", fontSize: "1rem" }}>暂无常用员工</div>}
            <div style={{ fontSize: "0.92rem", color: "#999", margin: "12px 0 4px" }}>搜索添加</div>
            <div style={{ display: "flex", gap: 8 }}>
              <Input placeholder="工号或姓名" value={searchKw} onChange={setSearchKw} onEnterPress={doSearch} />
              <Button size="small" onClick={doSearch}>搜索</Button>
            </div>
            <div style={{ marginTop: 8 }}>
              {searchResults.map((e) => {
                const added = chiefWorkers.some((w) => w.empId === e.empId);
                return (
                  <div key={e.empId} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0" }}>
                    <span>
                      {e.name ?? e.empId}
                      <span style={{ color: "#bbb", marginLeft: 6 }}>{e.empId}</span>
                      {employmentStatusBadge(e.employmentStatus)}
                    </span>
                    <Button size="mini" color="primary" fill="outline" disabled={added} onClick={() => void bindWorker(e.empId)}>
                      {added ? "已添加" : "添加"}
                    </Button>
                  </div>
                );
              })}
            </div>
          </Popup>

          {/* 添加工人：代签 / 加班 两 Tab */}
          <Popup
            visible={addOpen}
            onMaskClick={() => setAddOpen(false)}
            onClose={() => setAddOpen(false)}
            showCloseButton
            bodyStyle={{ padding: 16, maxHeight: "82vh", overflow: "auto" }}
          >
            <h4 style={{ marginTop: 0 }}>
              {addTab === "overtime"
                ? `添加加班次${pickerPost ? ` · ${pickerPost}` : ""}${pickerSquad ? ` · ${pickerSquad}` : ""}`
                : `添加操作工${pickerPost ? ` · ${pickerPost}` : ""}${pickerSquad ? ` · ${pickerSquad}` : ""}`}
            </h4>
            <Tabs activeKey={addTab} onChange={switchAddTab}>
              <Tabs.Tab title="代签" key="proxy">
                <div style={{ fontSize: "0.92rem", color: "#999", margin: "8px 0 4px" }}>常用员工</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {chiefWorkers.map((w) => (
                    <Button
                      key={w.empId}
                      size="small"
                      onClick={() => {
                        setAddOpen(false);
                        openProxy(w.empId, pickerPost, pickerSquad);
                      }}
                    >
                      {w.name ?? w.empId}（{w.empId}）
                      {employmentStatusBadge(w.employmentStatus)}
                    </Button>
                  ))}
                  {chiefWorkers.length === 0 && <span style={{ color: "#bbb", fontSize: "1rem" }}>暂无常用员工</span>}
                </div>
                <div style={{ fontSize: "0.92rem", color: "#999", margin: "12px 0 4px" }}>搜索其他员工</div>
                <div style={{ display: "flex", gap: 8 }}>
                  <Input placeholder="工号或姓名" value={searchKw} onChange={setSearchKw} onEnterPress={doSearch} />
                  <Button size="small" onClick={doSearch}>搜索</Button>
                </div>
                <div style={{ marginTop: 8 }}>
                  {searchResults.map((e) => (
                    <Button
                      key={e.empId}
                      block
                      style={{ marginBottom: 6, textAlign: "left" }}
                      onClick={() => {
                        setAddOpen(false);
                        openProxy(e.empId, pickerPost, pickerSquad);
                      }}
                    >
                      {e.name ?? e.empId}（{e.empId}）{e.role === "chief" ? " · 值班长" : ""}
                      {employmentStatusBadge(e.employmentStatus)}
                    </Button>
                  ))}
                </div>
              </Tabs.Tab>
              <Tabs.Tab title="加班次" key="overtime">
                {!otEmp ? (
                  <>
                    <div style={{ fontSize: "0.92rem", color: "#999", margin: "8px 0 4px" }}>
                      当天已有扫码记录的操作工（跨岗位加班次，须为不同班次）
                    </div>
                    {overtimeCandidates.length === 0 ? (
                      <div style={{ color: "#bbb", fontSize: "1rem" }}>暂无可加班的操作工</div>
                    ) : (
                      overtimeCandidates.map((c) => (
                        <Button
                          key={c.empId}
                          block
                          data-testid={`overtime-pick-${c.empId}`}
                          disabled={(c.remainingHours ?? MAX_WORK_HOURS) <= 0}
                          style={{ marginBottom: 6, textAlign: "left" }}
                          onClick={() => openOvertime(c)}
                        >
                          {c.name ?? c.empId}（{c.empId}）
                          {employmentStatusBadge(c.employmentStatus)}
                          <span style={{ color: "#999", marginLeft: 6, fontSize: "0.85rem" }}>
                        已占班组 {c.squads.length ? c.squads.join("、") : "不分班"}
                        {c.slots?.length ? ` ｜ 已占时段 ${c.slots.join("、")}` : ""}
                          </span>
                          {c.remainingHours != null && (
                            <span style={{ color: c.remainingHours > 0 ? "#999" : "#cf1322", marginLeft: 6, fontSize: "0.85rem" }}>
                              已用{c.usedHours ?? 0}h，剩余{c.remainingHours}h
                            </span>
                          )}
                        </Button>
                      ))
                    )}
                    <div style={{ fontSize: "0.92rem", color: "#999", margin: "12px 0 4px" }}>
                      搜索其他操作工（须当天已有记录）
                    </div>
                    <div style={{ display: "flex", gap: 8 }}>
                      <Input
                        placeholder="工号或姓名"
                        value={otSearchKw}
                        onChange={setOtSearchKw}
                        onEnterPress={doOtSearch}
                      />
                      <Button size="small" onClick={doOtSearch}>
                        搜索
                      </Button>
                    </div>
                    <div style={{ marginTop: 8 }}>
                      {otSearchResults.map((e) => (
                        <Button
                          key={e.empId}
                          block
                          data-testid={`overtime-search-${e.empId}`}
                          style={{ marginBottom: 6, textAlign: "left" }}
                          onClick={() => pickOvertimeSearched(e)}
                        >
                          {e.name ?? e.empId}（{e.empId}）{e.role === "chief" ? " · 值班长" : ""}
                          {employmentStatusBadge(e.employmentStatus)}
                        </Button>
                      ))}
                    </div>
                  </>
                ) : (
                  <div data-testid="overtime-form">
                    <div style={{ marginBottom: 8, fontSize: "1rem" }}>
                      <strong>{otEmp.name ?? otEmp.empId}</strong>
                      <span style={{ color: "#999", marginLeft: 6 }}>{otEmp.empId}</span>
                      {employmentStatusBadge(otEmp.employmentStatus)}
                      <span style={{ color: "#999", marginLeft: 6, fontSize: "0.85rem" }}>
                        已占班组 {otEmp.squads.length ? otEmp.squads.join("、") : "不分班"}
                        {otEmp.slots?.length ? ` ｜ 已占时段 ${otEmp.slots.join("、")}` : ""}
                      </span>
                      {otEmp.remainingHours != null && (
                        <span style={{ color: otRemainingHours > 0 ? "#999" : "#cf1322", marginLeft: 6, fontSize: "0.85rem" }}>
                          已用{otEmp.usedHours ?? 0}h，剩余{otRemainingHours}h
                        </span>
                      )}
                      <Button size="mini" fill="none" style={{ marginLeft: 8 }} onClick={() => setOtEmp(null)}>
                        重选
                      </Button>
                    </div>
                    <div style={{ fontSize: "0.85rem", color: "#999", marginBottom: 8 }}>
                      不分班岗请用「改时长」加班。
                    </div>
                    <div style={{ marginBottom: 12 }}>
                      <div style={{ marginBottom: 4 }}>加班时间段</div>
                      <Selector
                        options={SHIFT_SLOTS.map((s) => ({
                          label: s,
                          value: s,
                        }))}
                        value={otShiftSlot ? [otShiftSlot] : []}
                        onChange={(v) => setOtShiftSlot((v[0] as string) ?? "")}
                      />
                    </div>
                    <div style={{ marginBottom: 12 }}>
                      <div style={{ marginBottom: 4 }}>班组（默认本人当天班组，可改）</div>
                      {otSquadOptions.length === 0 ? (
                        <div style={{ color: "#bbb", fontSize: "0.92rem" }}>本工作点无分班编制</div>
                      ) : (
                        <Selector
                          options={otSquadOptions}
                          value={otSquad ? [otSquad] : []}
                          onChange={(v) => {
                            setOtSquad((v[0] as string) ?? null);
                            setOtPost("");
                          }}
                        />
                      )}
                    </div>
                    {otSquad && (
                      <div style={{ marginBottom: 12 }}>
                        <div style={{ marginBottom: 4 }}>岗位</div>
                        <Selector
                          options={otValidPosts.map((p) => ({ label: p, value: p }))}
                          value={otPost ? [otPost] : []}
                          onChange={(v) => v[0] && setOtPost(v[0] as string)}
                        />
                      </div>
                    )}
                    <div style={{ marginBottom: 12 }}>
                      <div style={{ marginBottom: 4 }}>用工类型</div>
                      <Selector
                        options={["正式", "实习", "待岗"].map((t) => ({ label: t, value: t }))}
                        value={[otType]}
                        onChange={(v) => v[0] && setOtType(v[0] as string)}
                      />
                    </div>
                    <div style={{ marginBottom: 12 }}>
                      <div style={{ marginBottom: 4 }}>本额外的班次工作时长（小时）</div>
                      <Input
                        type="number"
                        placeholder="如 4 / 8"
                        value={otHours}
                        onChange={(v) => setOtHours(capHoursInput(v, otRemainingHours))}
                        style={{
                          "--font-size": "1.1rem",
                          border: "1px solid #ddd",
                          borderRadius: 8,
                          padding: "6px 10px",
                        } as React.CSSProperties}
                      />
                      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                        <Button size="small" disabled={otRemainingHours < 4} onClick={() => setOtHours(String(Math.min(4, otRemainingHours)))}>半班4h</Button>
                        <Button size="small" disabled={otRemainingHours < 8} onClick={() => setOtHours(String(Math.min(8, otRemainingHours)))}>整班8h</Button>
                      </div>
                      {otRemainingHours <= 0 && (
                        <div style={{ color: "#cf1322", fontSize: "0.85rem", marginTop: 6 }}>
                          该员工当天实际工作时长已达 24h，不能继续加班
                        </div>
                      )}
                    </div>
                    {otCapacity && (
                      <div style={{ fontSize: "0.9rem", marginBottom: 8, color: otCapacity.full ? "#cf1322" : "#999" }}>
                        正编占用 {otCapacity.used}/{otCapacity.capacity}
                        {otCapacity.full ? " · 已满编" : ""}
                        {otFormalFull && <span>（正式岗已满，无法越编加班；可改实习/待岗）</span>}
                      </div>
                    )}
                    <Button
                      block
                      color="primary"
                      loading={otSubmitting}
                      disabled={!otSquad || !otShiftSlot || !otPost || otFormalFull}
                      data-testid="overtime-submit"
                      onClick={submitOvertime}
                    >
                      确认加班
                    </Button>
                  </div>
                )}
              </Tabs.Tab>
            </Tabs>
          </Popup>

          <div style={{ display: "flex", alignItems: "center", background: "#fff" }}>
            <Tabs activeKey={tab} onChange={setTab} style={{ flex: 1, minWidth: 0 }}>
              {dateTabs.map((d) => (
                <Tabs.Tab
                  key={d.key}
                  title={
                    <span style={{ whiteSpace: "nowrap" }}>
                      {d.label} {d.date.slice(5)}
                    </span>
                  }
                />
              ))}
              {customDate && <Tabs.Tab key="custom" title={<span>{customDate}</span>} />}
            </Tabs>
            <div
              data-testid="confirm-date-pick"
              onClick={() => setDatePickerVisible(true)}
              style={{
                padding: "0 12px",
                color: tab === "custom" ? "#1677FF" : "#999",
                display: "flex",
                alignItems: "center",
                gap: 4,
                fontSize: "1rem",
                whiteSpace: "nowrap",
              }}
            >
              <CalendarOutline fontSize={18} />
              选日期
            </div>
          </div>
          <DatePicker
            visible={datePickerVisible}
            onClose={() => setDatePickerVisible(false)}
            precision="day"
            min={new Date(2024, 0, 1)}
            max={demoCtx?.today ? new Date(`${demoCtx.today}T12:00:00`) : new Date()}
            value={customDate ? new Date(`${customDate}T12:00:00`) : undefined}
            title="选择查看日期"
            onConfirm={(v) => {
              const y = v.getFullYear();
              const m = String(v.getMonth() + 1).padStart(2, "0");
              const dd = String(v.getDate()).padStart(2, "0");
              setCustomDate(`${y}-${m}-${dd}`);
              setTab("custom");
            }}
          />
          <div data-testid="roster" style={{ margin: 12, padding: 12, background: "#fff", borderRadius: 8 }}>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>当前编制 · {date}</div>
            {!roster.hasRules ? (
              <div style={{ color: "#bbb", fontSize: "1rem" }}>暂无编制配置</div>
            ) : (
              <>
                {roster.squadless.length > 0 && (
                  <div style={{ marginBottom: 12 }}>
                    <div
                      style={{
                        fontSize: "0.92rem",
                        color: "#4f6480",
                        marginBottom: 6,
                        fontWeight: 600,
                      }}
                    >
                      不分班
                    </div>
                    {roster.squadless.map((g) => (
                      <RosterPostGroup
                        key={`sl-${g.post}`}
                        g={g}
                        squad={null}
                        onPick={openDetail}
                        onProxy={openAddWorker}
                        onBind={bindWorker}
                        mutationsDisabled={writeLocked}
                      />
                    ))}
                  </div>
                )}
                {roster.squadBlocks.length > 0 && (
                  <div style={{ marginTop: roster.squadless.length > 0 ? 10 : 0 }}>
                    <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
                      {roster.squadBlocks.map((b) => {
                        const tone = ROSTER_SQUAD_TONE[b.squad] ?? ROSTER_SQUAD_TONE["1班"];
                        const active = selectedRosterSquad === b.squad;
                        return (
                          <button
                            key={b.squad}
                            type="button"
                            onClick={() => setActiveRosterSquad(b.squad)}
                            style={{
                              flex: 1,
                              minHeight: 34,
                              borderRadius: 8,
                              border: `1px solid ${active ? tone.text : tone.border}`,
                              background: active ? tone.activeBg : tone.bg,
                              color: tone.text,
                              fontWeight: active ? 700 : 500,
                              fontSize: "0.95rem",
                            }}
                          >
                            {b.squad}
                          </button>
                        );
                      })}
                    </div>
                    {activeRosterBlock && (
                      <div
                        style={{
                          padding: "10px 10px 2px",
                          borderRadius: 8,
                          border: `1px solid ${activeRosterTone.border}`,
                          background: activeRosterTone.bg,
                        }}
                      >
                        {activeRosterBlock.posts.map((g) => (
                          <RosterPostGroup
                            key={`${activeRosterBlock.squad}-${g.post}`}
                            g={g}
                            squad={activeRosterBlock.squad}
                            onPick={openDetail}
                            onProxy={openAddWorker}
                            onBind={bindWorker}
                            mutationsDisabled={writeLocked}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
          <Popup
            visible={!!detailEmp}
            onMaskClick={() => setDetailEmp(null)}
            onClose={() => setDetailEmp(null)}
            showCloseButton
            data-testid="confirm-evidence-popup"
          >
            <div style={{ padding: 16, minHeight: "62vh", maxHeight: "88vh", overflow: "auto" }}>
              <h4 style={{ margin: "0 0 8px" }}>打卡证据</h4>
              {detailEmp && (
                <p style={{ fontSize: "1rem", color: "#666", margin: "0 0 8px" }}>
                  {detailEmp.name ?? detailEmp.empId} · {detailEmp.empId} · {date}
                  {detailEmp.post ? ` · ${detailEmp.post}` : ""}
                  {detailEmp.squad ? ` · ${detailEmp.squad}` : ""}
                </p>
              )}
              {detailEmp && (
                <div style={{ marginBottom: 12 }}>
                  {detailEmp.overtime ? rosterTag("一天多班次（加班）", "#d4380d", "#fff2e8") : null}
                  {(detailEmp.workHours ?? 8) > 8
                    ? rosterTag(
                        `班次延时 ${detailEmp.workHours}h（整班8h+延时${roundHours((detailEmp.workHours ?? 8) - 8)}h）`,
                        "#fa541c",
                        "#fff7e6",
                      )
                    : null}
                  {!detailEmp.overtime && (detailEmp.workHours ?? 8) <= 8 && isHalfShift(detailEmp.workHours)
                    ? rosterTag("半班（4h）", "#fa8c16", "#fff7e6")
                    : null}
                </div>
              )}
              {detailEmp && isChief && !locked && !writeLocked && (
                <div
                  data-testid="chief-scan-actions"
                  style={{ marginBottom: 12, padding: 10, background: "#fafafa", borderRadius: 8 }}
                >
                  <div style={{ fontSize: "0.92rem", color: "#999", marginBottom: 6 }}>值班长操作（先选择，再点「提交」生效）</div>
                  {!detailEmp.excluded && (
                    <div style={{ marginBottom: 8 }}>
                      <div style={{ fontSize: "0.92rem", color: "#999", marginBottom: 4 }}>
                        {detailEmp.overtime ? "修改加班时长（小时）" : "修改工作时长（小时）"}
                        ·改完即暂存，点「提交」生效；<b style={{ color: "#cf1322" }}>低于 8h 仅可 4h 或 0h，最高 24h</b>，0=撤回，&gt;8=班次延时
                      </div>
                      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                        <Input
                          style={{ width: 72, border: "1px solid #ddd", borderRadius: 6, padding: "2px 8px", textAlign: "center" }}
                          value={detailHours}
                          onChange={(v) => {
                            setDetailHours(v);
                            stageHours(v);
                          }}
                          type="number"
                          placeholder="如 12"
                        />
                        <Button size="mini" onClick={() => { setDetailHours("4"); stageHours("4"); }}>4小时</Button>
                        <Button size="mini" onClick={() => { setDetailHours("8"); stageHours("8"); }}>8小时</Button>
                        <Button size="mini" color="danger" fill="outline" onClick={() => { setDetailHours("0"); stageHours("0"); }}>0=撤回</Button>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                        <span style={{ fontSize: "0.85rem", color: "#666" }}>延班时长</span>
                        <Input
                          style={{ width: 60, border: "1px solid #ddd", borderRadius: 6, padding: "2px 8px", textAlign: "center" }}
                          value={String(Math.min(MAX_SHIFT_EXTENSION_HOURS, Math.max(0, Number(detailHours) - FULL_SHIFT_HOURS)))}
                          onChange={(v) => applyOt(Number(v))}
                          type="number"
                        />
                        <span style={{ fontSize: "0.85rem", color: "#999" }}>小时</span>
                        <Button size="mini" onClick={() => applyOt(Math.max(0, Number(detailHours) - FULL_SHIFT_HOURS) + 1)}>延班+1小时</Button>
                        <Button
                          size="mini"
                          onClick={() => {
                            const cur = Math.max(0, Number(detailHours) - FULL_SHIFT_HOURS);
                            if (cur > 0) applyOt(cur - 1);
                          }}
                        >
                          延班-1小时
                        </Button>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6 }}>
                        {Number(detailHours) > 8 && (
                          <span style={{ fontSize: "0.85rem", color: "#d4380d" }}>{overtimeHoursLabel(Number(detailHours))}</span>
                        )}
                        {pending?.kind === "fraction" && (
                          <span style={{ fontSize: "0.85rem", color: "#1677FF" }}>已暂存：{pending.label}</span>
                        )}
                      </div>
                    </div>
                  )}
                  {!detailEmp.overtime && (
                  <>
                  {detailEmp.excluded && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
                      <Button
                        size="small"
                        color="primary"
                        fill={pending?.kind === "restore" ? "solid" : "outline"}
                        onClick={() => setPending({ kind: "restore", label: "恢复打卡" })}
                      >
                        恢复打卡
                      </Button>
                  </div>
                  )}
                  {!detailEmp.excluded && (
                    <>
                      <div style={{ fontSize: "0.92rem", color: "#999", marginBottom: 4 }}>调整班次</div>
                      <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
                        {SQUADS.map((s) => (
                          <Button
                            key={s}
                            size="mini"
                            color={reassignSquad === s ? "primary" : "default"}
                            fill={reassignSquad === s ? "solid" : "outline"}
                            onClick={() => {
                              setReassignSquad(s);
                              if (pending?.kind === "reassign") setPending(null);
                            }}
                          >
                            {s}
                          </Button>
                        ))}
                      </div>
                      <div style={{ fontSize: "0.92rem", color: "#999", marginBottom: 4 }}>
                        调整到岗位（按上面所选班次，点击选择，提交后生效）
                      </div>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                        {posts.map((p) => {
                          const isCurrent = p === detailEmp.post && reassignSquad === detailEmp.squad;
                          const staged =
                            pending?.kind === "reassign" && pending.post === p && pending.squad === reassignSquad;
                          return (
                            <Button
                              key={p}
                              size="mini"
                              color={staged ? "primary" : "default"}
                              fill={staged ? "solid" : "outline"}
                              disabled={isCurrent}
                              onClick={() =>
                                setPending({
                                  kind: "reassign",
                                  post: p,
                                  squad: reassignSquad,
                                  label: `调整为 ${reassignSquad} ${p}`,
                                })
                              }
                            >
                              {p}
                              {isCurrent ? "（当前）" : ""}
                            </Button>
                          );
                        })}
                      </div>
                    </>
                  )}
                  </>
                  )}
                  <div style={{ marginTop: 12, borderTop: "1px solid #eee", paddingTop: 10 }}>
                    <div style={{ fontSize: "0.92rem", color: pending ? "#1677FF" : "#999", marginBottom: 6 }}>
                      {pending ? `待提交：${pending.label}` : "请选择一项操作后提交"}
                    </div>
                    <div style={{ display: "flex", gap: 8 }}>
                      <Button size="small" disabled={!pending} onClick={() => setPending(null)}>
                        取消
                      </Button>
                      <Button
                        size="small"
                        color="primary"
                        disabled={!pending}
                        data-testid="chief-scan-submit"
                        onClick={() => void submitPending()}
                      >
                        提交
                      </Button>
                    </div>
                  </div>
                </div>
              )}
              {detailLoading ? (
                <p style={{ color: "#888" }}>加载中…</p>
              ) : detailEvidence ? (
                <>
                  <p data-testid="evidence-recon-status" style={{ margin: "0 0 8px" }}>
                    <strong>对账状态：</strong>
                    {detailEvidence.status}
                    {detailEvidence.timeDiffMinutes != null
                      ? `（时差 ${detailEvidence.timeDiffMinutes} 分钟）`
                      : ""}
                    <br />
                    <strong>出勤工时：</strong>
                    {attendanceHoursLabel(detailEvidence.attendanceResultWorkHours)}
                  </p>
                  <p data-testid="evidence-laoqin" style={{ margin: "0 0 8px" }}>
                    <strong>劳勤打卡</strong>
                    {detailEvidence.laoqinPunches.length ? (
                      <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
                        {detailEvidence.laoqinPunches.map((p) => (
                          <li key={p.time}>
                            {p.time.slice(0, 19)} · {p.inoutType}
                            {p.source ? ` · ${p.source}` : ""}
                            {p.makeupReason ? ` · ${p.makeupReason}` : ""}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      "（无）"
                    )}
                  </p>
                  <p data-testid="evidence-scan" style={{ margin: 0 }}>
                    <strong>工作点扫码</strong>
                    {detailEvidence.scanRecords.length ? (
                      <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
                        {detailEvidence.scanRecords.map((s) => (
                          <li key={s.time}>
                            {s.time.slice(0, 19)} · {s.post}
                            {s.squad ? ` · ${s.squad}` : ""} · {s.employType} · {s.source}
                            {s.workHours != null ? ` · ${overtimeHoursLabel(s.workHours)}` : ""}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      "（无）"
                    )}
                  </p>
                  {(detailEvidence.overtimes ?? []).length > 0 && (
                    <p data-testid="evidence-overtime" style={{ margin: "8px 0 0" }}>
                      <strong style={{ color: "#d4380d" }}>加班</strong>
                      <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
                        {(detailEvidence.overtimes ?? []).map((o, i) => (
                          <li key={i}>
                            {o.squad ? `${o.squad} · ` : "不分班 · "}
                            {o.post} · {o.employType} · {overtimeHoursLabel(o.workHours)}
                          </li>
                        ))}
                      </ul>
                    </p>
                  )}
                </>
              ) : (
                <p style={{ color: "#888" }}>暂无证据数据</p>
              )}
            </div>
          </Popup>
          <Popup visible={proxyOpen} onMaskClick={() => setProxyOpen(false)} onClose={() => setProxyOpen(false)} showCloseButton>
            <div style={{ padding: 16 }}>
              <h4>代签到 · {proxyEmp}</h4>
              <div
                style={{
                  marginBottom: 12,
                  padding: "8px 12px",
                  borderRadius: 8,
                  background: proxyExhausted ? "#fff1f0" : "#f6ffed",
                  color: proxyExhausted ? "#cf1322" : "#389e0d",
                  fontSize: "1rem",
                }}
              >
                {proxyQuota == null
                  ? "额度加载中…"
                  : proxyExhausted
                    ? `本月代签 ${proxyQuota.used}/${proxyQuota.limit} · 余量 0，已达上限`
                    : `本月代签 ${proxyQuota.used}/${proxyQuota.limit} · 余量 ${proxyQuota.remaining}`}
                {proxyQuota != null && proxyQuota.extra > 0 && (
                  <span>（含临时加量 {proxyQuota.extra}）</span>
                )}
              </div>
              {proxyExhausted && (
                <div style={{ marginBottom: 12, fontSize: "0.92rem", color: "#cf1322" }}>
                  本月该员工代签已达上限（{proxyQuota?.limit} 次）。如确需出勤，请联系人事/统计临时加量，或走「补录审批 + 证据」。
                </div>
              )}
              <div style={{ marginBottom: 12 }}>
                <div style={{ marginBottom: 4 }}>班组</div>
                <Selector
                  options={proxySquadOptions}
                  value={[proxySquad]}
                  onChange={(v) => v[0] && setProxySquad(v[0] as string)}
                />
              </div>
              <div style={{ marginBottom: 12 }}>
                <div style={{ marginBottom: 4 }}>岗位</div>
                <Selector
                  options={proxyAllPosts.map((p) => ({
                    label: p,
                    value: p,
                    disabled: !proxyValidPosts.has(p),
                  }))}
                  value={[proxyPost]}
                  onChange={(v) => v[0] && setProxyPost(v[0] as string)}
                />
                {!proxyComboValid && (
                  <div style={{ marginTop: 4, fontSize: "0.92rem", color: "#cf1322" }}>
                    「{proxyPost}」在「{proxySquad}」无编制，请改选合适的班组或岗位
                  </div>
                )}
                {proxyComboValid && proxyCapacity && (
                  <div
                    style={{
                      marginTop: 4,
                      fontSize: "0.92rem",
                      color: proxyCapacity.full ? "#cf1322" : "#999",
                    }}
                  >
                    正编 {proxyCapacity.used}/{proxyCapacity.capacity}
                    {proxyCapacity.full ? " · 已满" : ""}
                  </div>
                )}
              </div>
              <div style={{ marginBottom: 12 }}>
                <div style={{ marginBottom: 4 }}>用工类型</div>
                <Selector
                  options={["正式", "实习", "待岗"].map((t) => ({ label: t, value: t }))}
                  value={[proxyType]}
                  onChange={(v) => {
                    if (!v[0]) return;
                    setProxyType(v[0] as string);
                  }}
                />
              </div>
              <div style={{ marginBottom: 12 }}>
                <div style={{ marginBottom: 4 }}>
                  出勤
                </div>
                <Selector
                  options={[
                    { label: "整班", value: "full" },
                    { label: "半个班", value: "half" },
                  ]}
                  value={[proxyHalf ? "half" : "full"]}
                  onChange={(v) => {
                    if (!v[0]) return;
                    setProxyHalf(v[0] === "half");
                  }}
                />
              </div>
              {proxyComboValid && proxyFormalFull && (
                <div style={{ marginBottom: 12, fontSize: "0.92rem", color: "#cf1322" }}>
                  该岗位正编已满，无法代签「正式」；如确需出勤，请将用工类型改为 实习/待岗。
                </div>
              )}
              <Button block color="primary" onClick={proxyScan} disabled={proxyBlocked}>
                {proxyExhausted
                  ? "已达上限，无法代签"
                  : !proxyComboValid
                    ? "班组与岗位不匹配"
                    : proxyFormalFull
                      ? "正编已满，无法代签正式"
                      : "确认代签到"}
              </Button>
            </div>
          </Popup>
          <div
            style={{
              position: "fixed",
              bottom: 0,
              // 桌面端跟随应用外壳 480px 居中，避免底部条铺满整屏宽度
              left: "50%",
              transform: "translateX(-50%)",
              width: "100%",
              maxWidth: 480,
              padding: 12,
              background: "#fff",
              boxShadow: "0 -1px 8px rgba(0,0,0,0.06)",
            }}
          >
            {writeLocked ? (
              <Button block disabled>
                本工作点本月已封账 · 请联系 HR/Admin 解锁
              </Button>
            ) : locked ? (
              <Button block fill="outline" color="primary" onClick={() => submit(false)}>
                该日已锁定 · {sections?.confirmedBy} —— 点此解锁修改
              </Button>
            ) : (
              <Button
                block
                color="primary"
                onClick={() => submit(true)}
                style={{ background: "#1677FF" }}
              >
                全部确认提交
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
