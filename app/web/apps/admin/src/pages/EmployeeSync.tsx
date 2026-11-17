import SyncLogButton from "../components/SyncLogButton";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Button, Card, Input, InputNumber, Popconfirm, Progress, Select, Space, Table,
  Tabs, Tag, Tooltip, message,
} from "antd";
import {
  getDingtalkEmployeeRaw,
  getDingtalkEmployeeSyncStatus,
  getDict,
  getPostCatalog,
  getRosterRoots,
  getTitleMap,
  getUnmappedTitles,
  importOneDingtalkEmployee,
  putRosterRoots,
  putTitleMap,
  startDingtalkEmployeeSync,
  transformDingtalkEmployees,
  type DingtalkEmpRaw,
  type DingtalkEmpSyncStatus,
  type DictItem,
} from "@fdc/api-client";
import EmployeeDifferencesPage from "./EmployeeDifferences";

type RootRow = { label: string; deptId: number | null; regionCode: string | null };
type TitleRow = { title: string; role: string; post: string };

const ROLE_OPTIONS = [
  { value: "", label: "（不设角色）" },
  { value: "chief", label: "值班长 chief" },
  { value: "leader", label: "班组长 leader" },
  { value: "repairer", label: "维修工 repairer" },
  { value: "worker", label: "操作工 worker" },
  { value: "stat", label: "统计员 stat" },
  { value: "hr", label: "人事专员 hr" },
  { value: "admin", label: "系统管理员 admin" },
];

function RawEmployeeSyncPage() {
  const [status, setStatus] = useState<DingtalkEmpSyncStatus | null>(null);
  const [items, setItems] = useState<DingtalkEmpRaw[]>([]);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [transforming, setTransforming] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 同步范围编辑
  const [editing, setEditing] = useState(false);
  const [roots, setRoots] = useState<RootRow[]>([]);
  const [regions, setRegions] = useState<DictItem[]>([]);
  const [savingRoots, setSavingRoots] = useState(false);

  const openEditor = async () => {
    try {
      const [rootResult, regionResult] = await Promise.all([
        getRosterRoots(),
        getDict("region"),
      ]);
      setRoots(rootResult.items.map((x) => ({
        label: x.label,
        deptId: x.deptId,
        regionCode: x.regionCode,
      })));
      setRegions(regionResult.items);
      setEditing(true);
    } catch (e) {
      message.error((e as Error).message || "加载同步范围失败");
    }
  };

  const setRootField = (idx: number, field: keyof RootRow, value: string | number | null) => {
    setRoots((prev) => prev.map((r, i) => (i === idx ? { ...r, [field]: value } : r)));
  };

  const saveRoots = async () => {
    const items = roots.map((r) => ({
      label: (r.label || "").trim(),
      deptId: Number(r.deptId),
      regionCode: r.regionCode || "",
    }));
    if (items.some((r) => !r.label)) return message.warning("描述不能为空");
    if (items.some((r) => !Number.isInteger(r.deptId) || r.deptId <= 0))
      return message.warning("部门 ID 必须为正整数");
    if (items.some((r) => !r.regionCode)) return message.warning("每个根部门都必须选择地区");
    if (items.length === 0) return message.warning("至少需要一个根部门");
    setSavingRoots(true);
    try {
      await putRosterRoots(items);
      message.success("同步范围已保存");
      setEditing(false);
      void pollStatus();
    } catch (e) {
      message.error((e as Error).message || "保存失败");
    } finally {
      setSavingRoots(false);
    }
  };

  const loadRaw = useCallback(async () => {
    setLoading(true);
    try {
      const r = await getDingtalkEmployeeRaw({ q: q || undefined, limit: 100000 });
      setItems(r.items);
    } catch (e) {
      message.error((e as Error).message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, [q]);

  const pollStatus = useCallback(async () => {
    try {
      const s = await getDingtalkEmployeeSyncStatus();
      setStatus(s);
      if (s.status !== "running" && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
        if (s.status === "done") {
          message.success("通讯录同步完成");
          void loadRaw();
        } else if (s.status === "error") {
          message.error("同步失败：" + (s.message || ""));
        }
      }
    } catch {
      /* ignore poll errors */
    }
  }, [loadRaw]);

  useEffect(() => {
    void pollStatus();
    void loadRaw();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(() => void pollStatus(), 2000);
  };

  const doSync = async () => {
    setStarting(true);
    try {
      const r = await startDingtalkEmployeeSync();
      if (r.alreadyRunning) message.info("同步已在进行中");
      else message.success("已开始后台同步");
      startPolling();
      void pollStatus();
    } catch (e) {
      message.error((e as Error).message || "启动失败");
    } finally {
      setStarting(false);
    }
  };

  const doTransform = async () => {
    setTransforming(true);
    try {
      const r = await transformDingtalkEmployees(false);
      let msg = `转换完成：新增 ${r.inserted}，更新 ${r.updated}（原始 ${r.rawTotal}）；`
        + `设角色 ${r.roleSet}，设常用职位 ${r.postSet}，设常用地区 ${r.regionSet}，绑定值班长 ${r.chiefBound}`;
      if (r.chiefUnmatched.length) msg += `；${r.chiefUnmatched.length} 名值班长未匹配到工作点`;
      message.success(msg, 8);
      if (r.chiefUnmatched.length)
        message.warning("未匹配工作点的值班长：" + r.chiefUnmatched.slice(0, 20).join("、"), 10);
    } catch (e) {
      message.error((e as Error).message || "转换失败");
    } finally {
      setTransforming(false);
    }
  };

  // 单人导入（用钉钉直接查某个 userid，无需先跑整体同步）
  const [importUserid, setImportUserid] = useState("");
  const [importingOne, setImportingOne] = useState(false);

  const doImportOne = async () => {
    const uid = importUserid.trim();
    if (!uid) return message.warning("请输入钉钉 userid");
    setImportingOne(true);
    try {
      const r = await importOneDingtalkEmployee(uid, false);
      const parts = [
        r.action === "inserted" ? "新增" : "更新",
        `工号 ${r.empId}`,
        r.name,
        r.role ? `角色 ${r.role}` : "未设角色",
      ];
      if (r.roleSet) parts.push("已按映射设角色");
      if (r.postSet) parts.push("已设常用职位");
      if (r.regionSet) parts.push("已设常用地区");
      if (r.chiefBound) parts.push("已绑定工作点");
      else if (r.chiefUnmatched) parts.push("值班长未匹配到工作点");
      message.success(parts.join("，"), 8);
      setImportUserid("");
      void loadRaw();
    } catch (e) {
      message.error((e as Error).message || "导入失败");
    } finally {
      setImportingOne(false);
    }
  };

  // 按行导入：表格每行单独把该员工落地花名册（复用单人导入接口）
  const [importingRow, setImportingRow] = useState<string | null>(null);
  const importRow = async (row: DingtalkEmpRaw) => {
    setImportingRow(row.userid);
    try {
      const r = await importOneDingtalkEmployee(row.userid, false);
      const parts = [
        r.action === "inserted" ? "新增" : "更新",
        `工号 ${r.empId}`,
        r.name,
        r.role ? `角色 ${r.role}` : "未设角色",
      ];
      if (r.roleSet) parts.push("已按映射设角色");
      if (r.postSet) parts.push("已设常用职位");
      if (r.regionSet) parts.push("已设常用地区");
      if (r.chiefBound) parts.push("已绑定工作点");
      else if (r.chiefUnmatched) parts.push("值班长未匹配到工作点");
      message.success(parts.join("，"), 8);
    } catch (e) {
      message.error((e as Error).message || "导入失败");
    } finally {
      setImportingRow(null);
    }
  };

  // 职位映射编辑
  const [editingTitles, setEditingTitles] = useState(false);
  const [titleMap, setTitleMap] = useState<TitleRow[]>([]);
  const [postOptions, setPostOptions] = useState<string[]>([]);
  const [savingTitles, setSavingTitles] = useState(false);

  const openTitleEditor = async () => {
    try {
      const [tm, pc] = await Promise.all([getTitleMap(), getPostCatalog()]);
      setTitleMap(tm.items.map((x) => ({ title: x.title, role: x.role, post: x.post })));
      setPostOptions(pc.items);
      setEditingTitles(true);
    } catch (e) {
      message.error((e as Error).message || "加载职位映射失败");
    }
  };

  const importUnmapped = async () => {
    try {
      const r = await getUnmappedTitles();
      const have = new Set(titleMap.map((t) => t.title));
      const add = r.items.filter((x) => !have.has(x.title)).map((x) => ({ title: x.title, role: "", post: "" }));
      if (!add.length) return message.info("没有未映射的新职位");
      setTitleMap((prev) => [...prev, ...add]);
      message.success(`已导入 ${add.length} 个未映射职位，请逐条配置后保存`);
    } catch (e) {
      message.error((e as Error).message || "导入失败");
    }
  };

  const setTitleField = (idx: number, field: keyof TitleRow, value: string) => {
    setTitleMap((prev) => prev.map((r, i) => (i === idx ? { ...r, [field]: value } : r)));
  };

  const saveTitles = async () => {
    const items = titleMap.map((t) => ({ title: (t.title || "").trim(), role: t.role || "", post: t.post || "" }));
    if (items.some((t) => !t.title)) return message.warning("钉钉职位不能为空");
    const titles = items.map((t) => t.title);
    if (new Set(titles).size !== titles.length) return message.warning("存在重复的钉钉职位");
    setSavingTitles(true);
    try {
      await putTitleMap(items);
      message.success("职位映射已保存");
      setEditingTitles(false);
    } catch (e) {
      message.error((e as Error).message || "保存失败");
    } finally {
      setSavingTitles(false);
    }
  };

  const running = status?.status === "running";
  const progressText = (() => {
    if (!status || status.status === "idle") return "尚未同步";
    if (status.status === "error") return status.message || "同步失败";
    if (status.status === "done") return status.message || "已完成";
    // running
    if (status.phase === "部门树")
      return `部门树同步中：${status.currentRoot ?? ""} 已 ${status.deptDone} 个部门`;
    if (status.phase === "人员")
      return `人员同步中：已 ${status.memberDoneDepts}/${status.memberTotalDepts} 部门，累计 ${status.memberCount} 人`;
    return status.message || status.phase || "进行中…";
  })();
  const percent = (() => {
    if (!status || status.status === "idle") return 0;
    if (status.status === "done") return 100;
    if (status.phase === "人员" && status.memberTotalDepts > 0)
      return Math.min(99, Math.round((status.memberDoneDepts / status.memberTotalDepts) * 100));
    if (status.phase === "部门树") return Math.min(40, 10 + status.deptDone % 30);
    return 5;
  })();

  const columns = [
    { title: "工号", dataIndex: "jobNumber", key: "jobNumber", width: 130, render: (v: string | null) => v || "-" },
    { title: "姓名", dataIndex: "name", key: "name", width: 120, render: (v: string | null) => v || "-" },
    { title: "userid", dataIndex: "userid", key: "userid", width: 200 },
    { title: "职位", dataIndex: "title", key: "title", width: 140, render: (v: string | null) => v || "-" },
    {
      title: "状态", dataIndex: "active", key: "active", width: 80,
      render: (v: boolean | null) => (v == null ? "-" : v ? <Tag color="green">在职</Tag> : <Tag>停用</Tag>),
    },
    { title: "归属", dataIndex: "rootLabel", key: "rootLabel", width: 160, render: (v: string | null) => v || "-" },
    {
      title: "部门路径", dataIndex: "deptPath", key: "deptPath", ellipsis: true,
      render: (v: string | null) => (v ? <Tooltip title={v}>{v}</Tooltip> : "-"),
    },
    {
      title: "操作", key: "_import", width: 130, fixed: "right" as const,
      render: (_: unknown, row: DingtalkEmpRaw) => (
        <Button
          size="small"
          type="primary"
          loading={importingRow === row.userid}
          disabled={running || (!!importingRow && importingRow !== row.userid)}
          onClick={() => void importRow(row)}
        >
          导入至花名册
        </Button>
      ),
    },
  ];

  if (editingTitles) {
    return (
      <Card
        title="编辑职位映射（钉钉职位 → 角色 / 常用职位）"
        extra={
          <Space>
            <Button onClick={importUnmapped} disabled={savingTitles}>导入未映射职位</Button>
            <Button onClick={() => setEditingTitles(false)} disabled={savingTitles}>取消</Button>
            <Button type="primary" loading={savingTitles} onClick={saveTitles}>保存</Button>
          </Space>
        }
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Alert
            type="info"
            showIcon
            message="转花名册时按钉钉职位命中本表：写入员工角色 + 常用职位（常用岗位收藏）。角色选「值班长」时，会按该员工钉钉部门（地区+末级工作点编号）自动绑定为对应工作点的值班长。角色/常用职位可留空表示不设。"
          />
          <Table
            rowKey={(_, i) => String(i)}
            size="small"
            pagination={false}
            scroll={{ y: 480 }}
            dataSource={titleMap.map((r, i) => ({ ...r, _i: i }))}
            columns={[
              {
                title: "钉钉职位", dataIndex: "title",
                render: (_: unknown, row: TitleRow & { _i: number }) => (
                  <Input
                    value={row.title}
                    placeholder="如：值班长（大工作点）"
                    onChange={(e) => setTitleField(row._i, "title", e.target.value)}
                  />
                ),
              },
              {
                title: "角色", dataIndex: "role", width: 200,
                render: (_: unknown, row: TitleRow & { _i: number }) => (
                  <Select
                    style={{ width: "100%" }}
                    value={row.role || ""}
                    options={ROLE_OPTIONS}
                    onChange={(v) => setTitleField(row._i, "role", v)}
                  />
                ),
              },
              {
                title: "常用职位", dataIndex: "post", width: 200,
                render: (_: unknown, row: TitleRow & { _i: number }) => (
                  <Select
                    style={{ width: "100%" }}
                    value={row.post || ""}
                    allowClear
                    placeholder="（不设）"
                    options={postOptions.map((p) => ({ value: p, label: p }))}
                    onChange={(v) => setTitleField(row._i, "post", v || "")}
                  />
                ),
              },
              {
                title: "操作", width: 70,
                render: (_: unknown, row: TitleRow & { _i: number }) => (
                  <Popconfirm
                    title="删除该映射？"
                    onConfirm={() => setTitleMap((prev) => prev.filter((_, i) => i !== row._i))}
                  >
                    <Button type="link" danger size="small">删除</Button>
                  </Popconfirm>
                ),
              },
            ]}
          />
          <Button onClick={() => setTitleMap((prev) => [...prev, { title: "", role: "", post: "" }])}>
            + 新增映射
          </Button>
        </Space>
      </Card>
    );
  }

  if (editing) {
    return (
      <Card
        title="编辑同步范围（根部门）"
        extra={
          <Space>
            <Button onClick={() => setEditing(false)} disabled={savingRoots}>取消</Button>
            <Button type="primary" loading={savingRoots} onClick={saveRoots}>保存</Button>
          </Space>
        }
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Alert
            type="info"
            showIcon
            message="同步通讯录时只抓取以下根部门下的部门树与人员。每个根部门必须对应一个基础地区；转为花名册时，该地区会写入员工的常用地区。"
          />
          <Table
            rowKey={(_, i) => String(i)}
            size="small"
            pagination={false}
            dataSource={roots.map((r, i) => ({ ...r, _i: i }))}
            columns={[
              {
                title: "描述", dataIndex: "label",
                render: (_: unknown, row: RootRow & { _i: number }) => (
                  <Input
                    value={row.label}
                    placeholder="如：示例厂区A生产部"
                    onChange={(e) => setRootField(row._i, "label", e.target.value)}
                  />
                ),
              },
              {
                title: "钉钉部门 ID", dataIndex: "deptId", width: 220,
                render: (_: unknown, row: RootRow & { _i: number }) => (
                  <InputNumber
                    style={{ width: "100%" }}
                    value={row.deptId}
                    min={1}
                    precision={0}
                    controls={false}
                    placeholder="如：100000001"
                    onChange={(v) => setRootField(row._i, "deptId", v)}
                  />
                ),
              },
              {
                title: "地区", dataIndex: "regionCode", width: 220,
                render: (_: unknown, row: RootRow & { _i: number }) => (
                  <Select
                    style={{ width: "100%" }}
                    value={row.regionCode || undefined}
                    placeholder="请选择基础地区"
                    options={regions.map((region) => ({
                      value: region.code,
                      label: `${region.name}（${region.code}）`,
                    }))}
                    onChange={(value) => setRootField(row._i, "regionCode", value)}
                  />
                ),
              },
              {
                title: "操作", width: 80,
                render: (_: unknown, row: RootRow & { _i: number }) => (
                  <Popconfirm
                    title="删除该根部门？"
                    onConfirm={() => setRoots((prev) => prev.filter((_, i) => i !== row._i))}
                  >
                    <Button type="link" danger size="small">删除</Button>
                  </Popconfirm>
                ),
              },
            ]}
          />
          <Button onClick={() => setRoots((prev) => [...prev, { label: "", deptId: null, regionCode: null }])}>
            + 新增根部门
          </Button>
        </Space>
      </Card>
    );
  }

  return (
    <Card
      title="通讯录同步"
      extra={
        <Space size="middle">
          <span style={{ color: "#888", fontSize: 12 }}>
            范围：{status?.roots && Object.keys(status.roots).length
              ? Object.keys(status.roots).join(" + ")
              : "示例厂区A/示例厂区B生产部"}
          </span>
          <Button size="small" disabled={running} onClick={openEditor}>编辑同步范围</Button>
          <Button size="small" disabled={running} onClick={openTitleEditor}>编辑职位映射</Button>
        </Space>
      }
    >
      <Space direction="vertical" style={{ width: "100%" }} size="middle">
        <Space wrap size={[8, 8]} align="center">
          <Button type="primary" loading={starting} disabled={running} onClick={doSync}>
            {running ? "同步进行中…" : "同步通讯录"}
          </Button>
          <Button loading={transforming} disabled={running} onClick={doTransform}>
            转为员工花名册
          </Button>
          <Input.Search
            placeholder="搜工号/姓名"
            allowClear
            style={{ width: 220 }}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onSearch={() => void loadRaw()}
          />
          <span style={{ color: "#888", fontSize: 12 }}>共 {items.length} 条原始员工</span>
        </Space>

        <Space wrap size={[8, 8]} align="center">
          <Tooltip title="无需先跑整体同步：用钉钉直接查询该 userid 落地花名册，并按职位映射设角色/常用职位、按根部门设置常用地区、值班长按部门绑定工作点">
            <Input
              placeholder="单人导入：钉钉 userid"
              allowClear
              style={{ width: 260 }}
              value={importUserid}
              disabled={running || importingOne}
              onChange={(e) => setImportUserid(e.target.value)}
              onPressEnter={() => void doImportOne()}
            />
          </Tooltip>
          <Button loading={importingOne} disabled={running} onClick={doImportOne}>
            导入该用户
          </Button>
        </Space>

        {status && status.status !== "idle" && (
          <div>
            <Progress
              percent={percent}
              status={status.status === "error" ? "exception" : status.status === "done" ? "success" : "active"}
            />
            <div style={{ color: status.status === "error" ? "#cf1322" : "#555", fontSize: 13 }}>
              {progressText}
            </div>
          </div>
        )}

        {status?.status === "error" && status.message && (
          <Alert type="error" showIcon message="同步失败" description={status.message} />
        )}

        <Table
          rowKey="userid"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={items}
          scroll={{ x: 1000 }}
          pagination={{
            defaultPageSize: 50,
            showSizeChanger: true,
            pageSizeOptions: ["20", "50", "100", "500", "1000"],
            showTotal: (t) => `共 ${t} 条`,
          }}
        />
      </Space>
    </Card>
  );
}

export default function EmployeeSyncPage() {
  return (
    <Tabs
      tabBarExtraContent={<SyncLogButton group="employee" />}
      defaultActiveKey="raw"
      items={[
        {
          key: "raw",
          label: "原始通讯录",
          children: <RawEmployeeSyncPage />,
        },
        {
          key: "differences",
          label: "员工差异",
          children: <EmployeeDifferencesPage />,
        },
      ]}
    />
  );
}
