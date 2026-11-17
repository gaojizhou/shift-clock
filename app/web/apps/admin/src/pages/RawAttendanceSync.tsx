import SyncLogButton from "../components/SyncLogButton";
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, DatePicker, Input, Popconfirm, Progress, Space, Spin, Table, Tabs, Tag, Tooltip, message } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import {
  getDingtalkAttendanceRaw,
  getDingtalkAttendanceSyncLogs,
  getDingtalkAttendanceTransformStatus,
  getExternalAttendanceResult,
  getExternalAttendanceResultStatus,
  pullDingtalkAttendance,
  pullExternalAttendanceResult,
  transformDingtalkAttendance,
  type AttendanceSyncLog,
  type AttendanceTransformStatus,
  type DingtalkAttRaw,
  type ExternalAttendanceResultRaw,
} from "@fdc/api-client";

const { RangePicker } = DatePicker;

export default function RawAttendanceSyncPage({ active = true }: { active?: boolean }) {
  return (
    <Card
      title="原始考勤同步"
      extra={
        <Space><SyncLogButton group="attendance" active={active} />
        <span style={{ color: "#888", fontSize: 12 }}>
          来源：外部钉钉考勤视图（只读）→ 转换后进入「劳勤工作台」 · 每天 0 点自动同步本月至今和上月全月
        </span></Space>
      }
    >
      <Tabs
        defaultActiveKey="raw"
        items={[
          { key: "raw", label: "原始考勤", children: <RawSyncTab active={active} /> },
          { key: "result", label: "考勤结果", children: <ResultSyncTab active={active} /> },
          { key: "logs", label: "历史考勤日志", children: <SyncLogTab active={active} /> },
        ]}
      />
    </Card>
  );
}

function RawSyncTab({ active }: { active: boolean }) {
  // 默认范围 = 当天
  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs(), dayjs()]);
  // 输入值与已应用条件分离：只有点击搜索或按 Enter 才把输入值用于查询。
  const [employeeIdInput, setEmployeeIdInput] = useState("");
  const [employeeNameInput, setEmployeeNameInput] = useState("");
  const [searchFilters, setSearchFilters] = useState({ employeeId: "", employeeName: "" });
  const [searchSeq, setSearchSeq] = useState(0);
  const [items, setItems] = useState<DingtalkAttRaw[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [transformStatus, setTransformStatus] = useState<AttendanceTransformStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const previousActive = useRef(active);
  const transforming = transformStatus?.status === "running";

  const fmt = (d: Dayjs) => d.format("YYYY-MM-DD");

  const load = useCallback(async () => {
    if (!range?.[0] || !range?.[1]) return;
    setLoading(true);
    try {
      const res = await getDingtalkAttendanceRaw({
        dateFrom: fmt(range[0]),
        dateTo: fmt(range[1]),
        limit: 1000,
        employeeId: searchFilters.employeeId || undefined,
        employeeName: searchFilters.employeeName || undefined,
      });
      setItems(res.items);
      setTotal(res.total ?? res.count ?? res.items.length);
    } catch (e) {
      message.error((e as Error).message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, [range, searchFilters]);

  const applySearch = () => {
    setSearchFilters({
      employeeId: employeeIdInput.trim(),
      employeeName: employeeNameInput.trim(),
    });
    // 同样的条件再次点击搜索时也要重新请求。
    setSearchSeq((value) => value + 1);
  };

  const resetSearch = () => {
    setEmployeeIdInput("");
    setEmployeeNameInput("");
    setSearchFilters({ employeeId: "", employeeName: "" });
    setSearchSeq((value) => value + 1);
  };

  useEffect(() => {
    void load();
  }, [load, searchSeq]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (!active && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    // 重激活除了刷新列表，还必须重查任务状态并接管轮询：
    // 本页常驻缓存挂载，失活时轮询已清除，若只 load() 会把 running 状态
    // 冻结在切走瞬间，按钮永久禁用、完成提示也不会到来。
    if (becameActive) {
      void load();
      void adoptStatus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, load]);

  // 按现存员工工号「拉取+转劳勤」后台任务：立即返回，进度走 transform-status 轮询（与转换共用状态机）
  const doPull = async () => {
    if (!range?.[0] || !range?.[1]) return;
    try {
      const r = await pullDingtalkAttendance({ dateFrom: fmt(range[0]), dateTo: fmt(range[1]) });
      if (!r.started && r.alreadyRunning) {
        message.warning("已有拉取/转换任务在进行中，请等待完成");
      } else {
        message.info("已启动后台拉取：按现存员工工号拉取原始考勤并转为劳勤，可在下方查看进度", 4);
      }
      await pollTransform();
      if (!pollRef.current) {
        pollRef.current = setInterval(() => void pollTransform(), 2000);
      }
    } catch (e) {
      message.error((e as Error).message || "启动拉取失败");
    }
  };

  // 转换进度轮询：running 时每 2 秒拉一次，done/error 时停止并提示结果
  const pollTransform = useCallback(async () => {
    try {
      const s = await getDingtalkAttendanceTransformStatus();
      setTransformStatus(s);
      if (s.status !== "running" && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
        if (s.status === "done") {
          message.success(s.message || "转换完成", 6);
          void load();
        } else if (s.status === "error") {
          message.error(s.message || "转换失败", 8);
        }
      }
    } catch {
      /* ignore poll errors */
    }
  }, [load]);

  // 查询一次最新任务状态；任务仍 running 且当前没有轮询时接管轮询。
  // 挂载与「失活后重激活」共用，避免常驻缓存页切回时状态冻结。
  const adoptStatus = useCallback(async () => {
    try {
      const s = await getDingtalkAttendanceTransformStatus();
      setTransformStatus(s);
      if (s.status === "running" && !pollRef.current) {
        pollRef.current = setInterval(() => void pollTransform(), 2000);
      }
    } catch {
      /* ignore status query errors */
    }
  }, [pollTransform]);

  // 进页面先查一次状态：若后台正在转换（如刷新/换人），自动接管轮询
  useEffect(() => {
    void adoptStatus();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const doTransform = async () => {
    if (!range?.[0] || !range?.[1]) return;
    try {
      const r = await transformDingtalkAttendance({ dateFrom: fmt(range[0]), dateTo: fmt(range[1]) });
      if (!r.started && r.alreadyRunning) {
        message.warning("已有转换任务在进行中，请等待完成");
      } else {
        message.info("已启动后台转换，按天推进，可在下方查看进度", 4);
      }
      // 立即拉一次并开始轮询
      await pollTransform();
      if (!pollRef.current) {
        pollRef.current = setInterval(() => void pollTransform(), 2000);
      }
    } catch (e) {
      message.error((e as Error).message || "启动转换失败");
    }
  };

  const columns = [
    { title: "工号", dataIndex: "code", key: "code", width: 130 },
    { title: "姓名", dataIndex: "name", key: "name", width: 120, render: (v: string | null) => v || "-" },
    { title: "打卡时刻", dataIndex: "attTime", key: "attTime", width: 175, render: (v: string | null) => v || "-" },
    {
      title: "方向", dataIndex: "direct", key: "direct", width: 90,
      render: (v: string | null) => (v ? <Tag>{v}</Tag> : "-"),
    },
    { title: "状态", dataIndex: "state", key: "state", width: 70, render: (v: string | null) => v || "-" },
    { title: "组织", dataIndex: "org", key: "org", width: 120, render: (v: string | null) => v || "-" },
    {
      title: "组织归属", dataIndex: "layer", key: "layer", ellipsis: true,
      render: (v: string | null) => (v ? <Tooltip title={v}>{v}</Tooltip> : "-"),
    },
    { title: "来源", dataIndex: "src", key: "src", width: 100, render: (v: string | null) => v || "-" },
    { title: "同步时间", dataIndex: "syncedAt", key: "syncedAt", width: 175, render: (v: string | null) => v || "-" },
  ];

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
        <Space wrap size={[8, 8]} align="center">
          <RangePicker
            allowClear={false}
            value={range}
            onChange={(v) => v && v[0] && v[1] && setRange([v[0], v[1]])}
          />
          <Input
            allowClear
            placeholder="员工ID（模糊）"
            value={employeeIdInput}
            onChange={(event) => setEmployeeIdInput(event.target.value)}
            onPressEnter={applySearch}
            style={{ width: 150 }}
            data-testid="raw-attendance-employee-id"
          />
          <Input
            allowClear
            placeholder="员工姓名（模糊）"
            value={employeeNameInput}
            onChange={(event) => setEmployeeNameInput(event.target.value)}
            onPressEnter={applySearch}
            style={{ width: 160 }}
            data-testid="raw-attendance-employee-name"
          />
          <Button
            type="primary"
            loading={loading}
            disabled={loading}
            onClick={applySearch}
            data-testid="raw-attendance-search"
          >
            搜索
          </Button>
          <Button disabled={loading} onClick={resetSearch}>重置</Button>
          <Tooltip title="按本地现存员工工号逐个拉取该范围的钉钉原始考勤，并转为劳勤（含对账）">
            <Button loading={transforming} disabled={transforming} onClick={doPull}>
              {transforming ? "任务进行中…" : "拉取并转劳勤"}
            </Button>
          </Tooltip>
          <Popconfirm
            title="转为劳勤考勤"
            description="仅把已入库的原始考勤按天转为劳勤打卡并对账（CSV 导入后用这个；「拉取并转劳勤」已包含此步骤）。"
            okText="确定转换"
            cancelText="取消"
            onConfirm={doTransform}
            disabled={transforming}
          >
            <Button disabled={transforming}>转为劳勤考勤</Button>
          </Popconfirm>
          <Button onClick={() => void load()}>刷新</Button>
          <span style={{ color: "#888", fontSize: 12 }}>
            现有 {total} 条原始记录{items.length < total ? `，已加载 ${items.length} 条` : ""}
          </span>
        </Space>

        {transformStatus && transformStatus.status !== "idle" && (
          <Alert
            type={
              transformStatus.status === "error"
                ? "error"
                : transformStatus.status === "done"
                  ? "success"
                  : "info"
            }
            showIcon
            message={
              transformStatus.status === "running"
                ? `后台任务进行中：${transformStatus.phase ?? ""}（${transformStatus.done}/${transformStatus.total}）`
                : transformStatus.message || ""
            }
            description={
              transformStatus.status === "running" ? (
                <Progress percent={transformStatus.percent} status="active" />
              ) : undefined
            }
            closable={transformStatus.status !== "running"}
            onClose={() => setTransformStatus(null)}
          />
        )}

        {loading ? (
          <div style={{ textAlign: "center", padding: 48 }}>
            <Spin />
          </div>
        ) : (
          <Table
            rowKey="id"
            size="small"
            columns={columns}
            dataSource={items}
            scroll={{ x: 1200 }}
            pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
          />
        )}
    </Space>
  );
}

function ResultSyncTab({ active }: { active: boolean }) {
  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs(), dayjs()]);
  const [userCode, setUserCode] = useState("");
  const [userName, setUserName] = useState("");
  const [deptName, setDeptName] = useState("");
  const [items, setItems] = useState<ExternalAttendanceResultRaw[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<AttendanceTransformStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const previousActive = useRef(active);
  const running = status?.status === "running";
  const fmt = (d: Dayjs) => d.format("YYYY-MM-DD");

  const load = useCallback(async () => {
    if (!range?.[0] || !range?.[1]) return;
    setLoading(true);
    try {
      const res = await getExternalAttendanceResult({
        dateFrom: fmt(range[0]),
        dateTo: fmt(range[1]),
        limit: 1000,
        userCode: userCode.trim() || undefined,
        userName: userName.trim() || undefined,
        deptName: deptName.trim() || undefined,
      });
      setItems(res.items);
      setTotal(res.total ?? res.count ?? res.items.length);
    } catch (e) {
      message.error((e as Error).message || "加载考勤结果失败");
    } finally {
      setLoading(false);
    }
  }, [range, userCode, userName, deptName]);

  const resetFilters = () => {
    setUserCode("");
    setUserName("");
    setDeptName("");
  };

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (!active && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    // 重激活必须重查任务状态并接管轮询，原因同原始考勤 tab：
    // 常驻缓存页失活时轮询被清除，只 load() 会让状态冻结、按钮永久禁用。
    if (becameActive) {
      void load();
      void adoptStatus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, load]);

  const poll = useCallback(async () => {
    try {
      const s = await getExternalAttendanceResultStatus();
      setStatus(s);
      if (s.status !== "running" && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
        if (s.status === "done") {
          message.success(s.message || "考勤结果拉取完成", 6);
          void load();
        } else if (s.status === "error") {
          message.error(s.message || "考勤结果拉取失败", 8);
        }
      }
    } catch {
      /* ignore poll errors */
    }
  }, [load]);

  // 查询一次最新任务状态；任务仍 running 且当前没有轮询时接管轮询。
  const adoptStatus = useCallback(async () => {
    try {
      const s = await getExternalAttendanceResultStatus();
      setStatus(s);
      if (s.status === "running" && !pollRef.current) {
        pollRef.current = setInterval(() => void poll(), 2000);
      }
    } catch {
      /* ignore status query errors */
    }
  }, [poll]);

  // 只在挂载时接管一次轮询：effect 依赖 adoptStatus 会随筛选输入（load 依赖
  // range/userCode/...）每次按键重跑，cleanup 清掉定时器后 pollRef 仍非空，
  // adoptStatus 的 !pollRef.current 判断为假，轮询永久停止、按钮一直禁用。
  const adoptStatusRef = useRef(adoptStatus);
  adoptStatusRef.current = adoptStatus;
  useEffect(() => {
    void adoptStatusRef.current();
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const doPull = async () => {
    if (!range?.[0] || !range?.[1]) return;
    try {
      const r = await pullExternalAttendanceResult({ dateFrom: fmt(range[0]), dateTo: fmt(range[1]) });
      if (!r.started && r.alreadyRunning) {
        message.warning("已有拉取/转换任务在进行中，请等待完成");
      } else {
        message.info("已启动后台拉取：按各自然月有工作点有效/待审记录的员工拉取考勤结果", 5);
      }
      await poll();
      if (!pollRef.current) {
        pollRef.current = setInterval(() => void poll(), 2000);
      }
    } catch (e) {
      message.error((e as Error).message || "启动考勤结果拉取失败");
    }
  };

  const columns = [
    { title: "工号", dataIndex: "userCode", key: "userCode", width: 130, fixed: "left" as const },
    { title: "姓名", dataIndex: "userName", key: "userName", width: 110, fixed: "left" as const, render: (v: string | null) => v || "-" },
    { title: "数据状态", key: "sourceState", width: 130, render: (_: unknown, row: ExternalAttendanceResultRaw) => row.sourceWithdrawnAt
      ? <Tooltip title={`撤回时间：${row.sourceWithdrawnAt}；以下为历史值，不参与计算`}><Tag color="default">源端已撤回</Tag></Tooltip>
      : <Tag color="success">有效</Tag> },
    { title: "考勤日期", dataIndex: "kqDate", key: "kqDate", width: 120 },
    {
      title: "状态", dataIndex: "statusName", key: "statusName", width: 90,
      render: (v: string | null) => (v ? <Tag color={v === "正常" ? "success" : "warning"}>{v}</Tag> : "-"),
    },
    { title: "上班", dataIndex: "actualOn", key: "actualOn", width: 170, render: (v: string | null) => v || "-" },
    { title: "下班", dataIndex: "actualOff", key: "actualOff", width: 170, render: (v: string | null) => v || "-" },
    { title: "应出勤", dataIndex: "quota", key: "quota", width: 90, render: (v: string | null) => v || "-" },
    { title: "出勤工时", dataIndex: "cqgs", key: "cqgs", width: 90, render: (v: string | null) => v || "-" },
    { title: "旷工", dataIndex: "kggs", key: "kggs", width: 80, render: (v: string | null) => v || "-" },
    { title: "迟到", dataIndex: "cd2", key: "cd2", width: 80, render: (v: string | null) => v || "-" },
    { title: "早退", dataIndex: "zt2", key: "zt2", width: 80, render: (v: string | null) => v || "-" },
    { title: "年休假（小时）", dataIndex: "leave01", key: "leave01", width: 140, render: (v: string | null) => v || "-" },
    { title: "排休假（小时）", dataIndex: "leave02", key: "leave02", width: 140, render: (v: string | null) => v || "-" },
    { title: "事假（小时）", dataIndex: "leave03", key: "leave03", width: 130, render: (v: string | null) => v || "-" },
    { title: "全薪病假（小时）", dataIndex: "leave04", key: "leave04", width: 150, render: (v: string | null) => v || "-" },
    { title: "病假", dataIndex: "leave05", key: "leave05", width: 90, render: (v: string | null) => v || "-" },
    { title: "计生假", dataIndex: "leave06", key: "leave06", width: 90, render: (v: string | null) => v || "-" },
    { title: "婚假", dataIndex: "leave07", key: "leave07", width: 90, render: (v: string | null) => v || "-" },
    { title: "产假", dataIndex: "leave09", key: "leave09", width: 90, render: (v: string | null) => v || "-" },
    { title: "陪产假", dataIndex: "leave10", key: "leave10", width: 90, render: (v: string | null) => v || "-" },
    { title: "丧假", dataIndex: "leave12", key: "leave12", width: 90, render: (v: string | null) => v || "-" },
    { title: "工伤假", dataIndex: "leave13", key: "leave13", width: 90, render: (v: string | null) => v || "-" },
    { title: "出差", dataIndex: "business02", key: "business_02", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "business_01", dataIndex: "business01", key: "business_01", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "ext1", dataIndex: "ext1", key: "ext1", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "ext2", dataIndex: "ext2", key: "ext2", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "ot1", dataIndex: "ot1", key: "ot1", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "ot2", dataIndex: "ot2", key: "ot2", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "ot3", dataIndex: "ot3", key: "ot3", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "hr_status", dataIndex: "hrStatus", key: "hr_status", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "hr_type", dataIndex: "hrType", key: "hr_type", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "lx_type", dataIndex: "lxType", key: "lx_type", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "kqqs", dataIndex: "kqqs", key: "kqqs", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "tenant_code", dataIndex: "tenantCode", key: "tenant_code", width: 120, render: (v: string | null) => v ?? "-" },
    { title: "kq_date_show", dataIndex: "kqDateShow", key: "kq_date_show", width: 180, render: (v: string | null) => v ?? "-" },
    { title: "kq_date_title", dataIndex: "kqDateTitle", key: "kq_date_title", width: 180, render: (v: string | null) => v ?? "-" },
    {
      title: "部门", dataIndex: "deptName", key: "deptName", width: 150,
      render: (v: string | null) => v || "-",
    },
    {
      title: "组织归属", dataIndex: "layerName", key: "layerName", ellipsis: true,
      render: (v: string | null) => (v ? <Tooltip title={v}>{v}</Tooltip> : "-"),
    },
    { title: "同步时间", dataIndex: "syncedAt", key: "syncedAt", width: 175, render: (v: string | null) => v || "-" },
  ];

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Space wrap size={[8, 8]} align="center">
        <RangePicker
          allowClear={false}
          value={range}
          onChange={(v) => v && v[0] && v[1] && setRange([v[0], v[1]])}
        />
        <Input
          allowClear
          placeholder="ID/工号"
          value={userCode}
          onChange={(e) => setUserCode(e.target.value)}
          onPressEnter={() => void load()}
          style={{ width: 140 }}
        />
        <Input
          allowClear
          placeholder="姓名（模糊）"
          value={userName}
          onChange={(e) => setUserName(e.target.value)}
          onPressEnter={() => void load()}
          style={{ width: 140 }}
        />
        <Input
          allowClear
          placeholder="部门（模糊）"
          value={deptName}
          onChange={(e) => setDeptName(e.target.value)}
          onPressEnter={() => void load()}
          style={{ width: 180 }}
        />
        <Tooltip title="按本地现存员工工号逐个拉取 view_external_att_result，并幂等写入后台考勤结果表">
          <Button type="primary" loading={running} disabled={running} onClick={doPull}>
            {running ? "任务进行中…" : "拉取考勤结果"}
          </Button>
        </Tooltip>
        <Button onClick={() => void load()}>刷新</Button>
        <Button onClick={resetFilters}>重置筛选</Button>
        <span style={{ color: "#888", fontSize: 12 }}>
          现有 {total} 条考勤结果{items.length < total ? `，已加载 ${items.length} 条` : ""}
        </span>
      </Space>

      {status && status.status !== "idle" && (
        <Alert
          type={status.status === "error" ? "error" : status.status === "done" ? "success" : "info"}
          showIcon
          message={
            status.status === "running"
              ? `后台任务进行中：${status.phase ?? ""}（${status.done}/${status.total}）`
              : status.message || ""
          }
          description={status.status === "running" ? <Progress percent={status.percent} status="active" /> : undefined}
          closable={status.status !== "running"}
          onClose={() => setStatus(null)}
        />
      )}

      <Alert type="info" showIcon message="标记为“源端已撤回”的记录仅供历史追溯，旧工时和旷工不再参与计算。同步失败时保留原数据。" />
      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}><Spin /></div>
      ) : (
        <Table
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={items}
          scroll={{ x: "max-content" }}
          pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
        />
      )}
    </Space>
  );
}

function SyncLogTab({ active }: { active: boolean }) {
  const [logs, setLogs] = useState<AttendanceSyncLog[]>([]);
  const [loading, setLoading] = useState(false);
  const previousActive = useRef(active);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await getDingtalkAttendanceSyncLogs(100);
      setLogs(r.items);
    } catch (e) {
      message.error((e as Error).message || "加载同步日志失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) void load();
  }, [active, load]);

  const columns = [
    {
      title: "时间", dataIndex: "ts", key: "ts", width: 165,
      render: (v: string | null) => (v ? v.slice(0, 19).replace("T", " ") : "-"),
    },
    {
      title: "触发", dataIndex: "trigger", key: "trigger", width: 80,
      render: (v: string | null) =>
        v === "auto" ? <Tag color="blue">定时</Tag> : v === "manual" ? <Tag>手动</Tag> : (v || "-"),
    },
    {
      title: "结果", dataIndex: "ok", key: "ok", width: 80,
      render: (v: boolean | null) => (v ? <Tag color="success">成功</Tag> : <Tag color="error">失败</Tag>),
    },
    {
      title: "日期范围", key: "range", width: 190,
      render: (_: unknown, r: AttendanceSyncLog) => `${r.dateFrom ?? "?"} ~ ${r.dateTo ?? "?"}`,
    },
    { title: "拉取", dataIndex: "fetched", key: "fetched", width: 70, render: (v: number | null) => v ?? "-" },
    { title: "入库", dataIndex: "upserted", key: "upserted", width: 70, render: (v: number | null) => v ?? "-" },
    { title: "结果入库", dataIndex: "attendanceResultUpserted", key: "aru", width: 90, render: (v: number | null) => v ?? "-" },
    { title: "转劳勤", dataIndex: "transformInserted", key: "ti", width: 70, render: (v: number | null) => v ?? "-" },
    { title: "对账", dataIndex: "matched", key: "matched", width: 70, render: (v: number | null) => v ?? "-" },
    { title: "操作人", dataIndex: "actor", key: "actor", width: 110 },
    {
      title: "错误", dataIndex: "error", key: "error", ellipsis: true,
      render: (v: string | null) => (v ? <Tooltip title={v}><span style={{ color: "#cf1322" }}>{v}</span></Tooltip> : "-"),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <Space>
        <Button onClick={() => void load()}>刷新</Button>
        <span style={{ color: "#888", fontSize: 12 }}>
          每天 0 点自动同步本月至今和上月全月；手动「拉取并转劳勤」也会记录在此。共 {logs.length} 条
        </span>
      </Space>
      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}><Spin /></div>
      ) : (
        <Table
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={logs}
          scroll={{ x: 1200 }}
          pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
        />
      )}
    </Space>
  );
}
