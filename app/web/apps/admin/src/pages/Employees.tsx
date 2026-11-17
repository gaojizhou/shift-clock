import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  AutoComplete,
  Button,
  Drawer,
  Input,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import {
  addChiefWorker,
  createEmployee,
  deleteChiefWorker,
  deleteEmployee,
  getAdminEmployee,
  getAdminEmployees,
  getChiefDepartedEmployeeVisibility,
  getChiefWorkers,
  getFurnaces,
  getMe,
  getPostCatalog,
  getRoles,
  searchEmployees,
  setChiefDepartedEmployeeVisibility,
  updateEmployee,
  type AdminEmployee,
  type ChiefWorker,
} from "@fdc/api-client";
import { useListCols, fmtTime } from "../useListCols";
import type { ColumnsType } from "antd/es/table";
import { usePageCache } from "../pageCache";

// 兜底项：角色字典加载完成前/失败时使用；正常以基础字典(getRoles)为准，避免漏映射。
const ROLE_OPTS_FALLBACK = [
  { value: "worker", label: "操作工" },
  { value: "chief", label: "值班长" },
  { value: "hr", label: "人事专员" },
  { value: "stat", label: "统计员" },
  { value: "admin", label: "系统管理员" },
  { value: "repairer", label: "维修工" },
  { value: "leader", label: "班组长" },
];

type Form = {
  empId: string;
  name: string;
  ddUserId: string;
  role: string;
  employmentStatus: "active" | "departed";
  pushEnabled: boolean;
  region: string[];
  furnace: string[];
  post: string[];
};

const EMPTY: Form = { empId: "", name: "", ddUserId: "", role: "worker", employmentStatus: "active", pushEnabled: false, region: [], furnace: [], post: [] };

export default function EmployeesPage({ active = true }: { active?: boolean }) {
  const { invalidate } = usePageCache();
  const previousActive = useRef(active);
  const [items, setItems] = useState<AdminEmployee[]>([]);
  const [kw, setKw] = useState("");
  const [roleFilters, setRoleFilters] = useState<string[]>([]);
  const [regionFilters, setRegionFilters] = useState<string[]>([]);
  const [pushFilters, setPushFilters] = useState<boolean[]>([]);
  const [employmentStatusFilter, setEmploymentStatusFilter] = useState<
    AdminEmployee["employmentStatus"]
  >();
  const [regions, setRegions] = useState<string[]>([]);
  const [furnaces, setFurnaces] = useState<string[]>([]);
  const [posts, setPosts] = useState<string[]>([]);
  const [roleOpts, setRoleOpts] = useState(ROLE_OPTS_FALLBACK);
  const roleLabel = useMemo(
    () => Object.fromEntries(roleOpts.map((o) => [o.value, o.label])) as Record<string, string>,
    [roleOpts]
  );

  const [open, setOpen] = useState(false);
  const [isNew, setIsNew] = useState(false);
  const [form, setForm] = useState<Form>(EMPTY);
  const [chiefWorkers, setChiefWorkers] = useState<ChiefWorker[]>([]);
  const [belongChiefs, setBelongChiefs] = useState<ChiefWorker[]>([]);
  const [wkOptions, setWkOptions] = useState<{ value: string; label: string }[]>([]);
  const [wkPick, setWkPick] = useState<string>();
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!active) setOpen(false);
  }, [active]);

  const { prefs, Toggles } = useListCols("employees", { id: true });
  const [actorRole, setActorRole] = useState("");
  const [actorRegion, setActorRegion] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [showDepartedToChief, setShowDepartedToChief] = useState(true);
  const [visibilitySaving, setVisibilitySaving] = useState(false);
  const reload = useCallback(() => {
    setLoading(true);
    return getAdminEmployees({
      q: kw,
      regions: regionFilters,
      roles: roleFilters,
      pushEnabled: pushFilters,
      employmentStatus: employmentStatusFilter,
    })
      .then((r) => setItems(r.items))
      .finally(() => setLoading(false));
  }, [employmentStatusFilter, kw, pushFilters, regionFilters, roleFilters]);
  useEffect(() => {
    getMe().then((m) => {
      setActorRole(m.role);
      setActorRegion(m.region);
      if ((m.role === "hr" || m.role === "stat") && m.region) {
        setRegionFilters([m.region]);
      }
      if (m.role === "hr" || m.role === "admin") {
        void getChiefDepartedEmployeeVisibility()
          .then((result) => setShowDepartedToChief(result.showDepartedToChief))
          .catch(() => undefined);
      }
    }).catch(() => setActorRole(""));
    getFurnaces().then((r) => {
      setRegions([...new Set(r.items.map((f) => f.region))]);
      setFurnaces([...new Set(r.items.map((f) => f.furnaceNo))]);
    });
    getPostCatalog().then((r) => setPosts(r.items));
    getRoles()
      .then((r) => r.items.length && setRoleOpts(r.items.map((x) => ({ value: x.code, label: x.name }))))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 200);
    return () => window.clearTimeout(timer);
  }, [reload]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) {
      invalidate("/employees");
      void reload();
    }
  }, [active, invalidate, reload]);

  const protectedRoles = new Set(["admin", "hr", "stat"]);
  const canEdit = (role: string) => actorRole === "admin" || (actorRole === "hr" && !protectedRoles.has(role));
  const formEditable = isNew ? actorRole === "admin" : canEdit(form.role);
  const editableRoleOpts = actorRole === "hr"
    ? roleOpts.filter((o) => !protectedRoles.has(o.value))
    : roleOpts;

  const set = (p: Partial<Form>) => setForm((f) => ({ ...f, ...p }));

  const toggleChiefDepartedVisibility = async (checked: boolean) => {
    setVisibilitySaving(true);
    try {
      const result = await setChiefDepartedEmployeeVisibility(checked);
      setShowDepartedToChief(result.showDepartedToChief);
      message.success(
        result.showDepartedToChief
          ? "值班长已可见离职员工"
          : "值班长已隐藏离职员工"
      );
    } catch {
      message.error("保存值班长可见性失败");
    } finally {
      setVisibilitySaving(false);
    }
  };

  const openNew = () => {
    setForm(EMPTY);
    setChiefWorkers([]);
    setBelongChiefs([]);
    setIsNew(true);
    setOpen(true);
  };

  const openEdit = async (emp: AdminEmployee) => {
    const d = await getAdminEmployee(emp.empId);
    setForm({
      empId: d.empId,
      name: d.name,
      ddUserId: d.ddUserId ?? "",
      role: d.role,
      employmentStatus: d.employmentStatus,
      pushEnabled: !!d.pushEnabled,
      // 保留历史多地区原值，让管理员显式选择唯一地区，禁止打开抽屉即静默裁剪。
      region: d.favorites.region,
      furnace: d.favorites.furnace,
      post: d.favorites.post,
    });
    setChiefWorkers(d.chiefWorkers ?? []);
    setBelongChiefs(d.belongChiefs ?? []);
    setIsNew(false);
    setOpen(true);
  };

  const save = async () => {
    if (!form.empId.trim() || !form.name.trim()) {
      message.warning("工号与姓名必填");
      return;
    }
    if ((form.role === "hr" || form.role === "stat") && form.region.length > 1) {
      message.warning("HR/统计只能配置一个所在地区，请先明确选择唯一地区");
      return;
    }
    setSaving(true);
    try {
      if (isNew) {
        await createEmployee({
          empId: form.empId.trim(),
          name: form.name.trim(),
          ddUserId: form.ddUserId.trim() || undefined,
          role: form.role,
          favorites: { region: form.region, furnace: form.furnace, post: form.post },
        });
      } else {
        await updateEmployee(form.empId, {
          name: form.name.trim(),
          ...(actorRole === "admin" ? { ddUserId: form.ddUserId.trim() } : {}),
          role: form.role,
          pushEnabled: form.pushEnabled,
          favorites: { region: form.region, furnace: form.furnace, post: form.post },
        });
      }
      message.success("已保存");
      await reload();
      setOpen(false);
    } catch (e) {
      const msg = (e as { data?: { detail?: string } }).data?.detail || "保存失败";
      message.error(msg);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (empId: string) => {
    await deleteEmployee(empId);
    message.success("已删除");
    reload();
  };


  // 值班长关联工人（仅编辑现有值班长时可用）
  const searchWorkers = async (q: string) => {
    const s = q.trim();
    if (!s) return setWkOptions([]);
    // 改用 searchEmployees：带 20 条上限且不做地区裁剪，避免无地区/跨地区工人被整条剔除。
    const r = await searchEmployees(s);
    setWkOptions(r.items.map((e) => ({ value: e.empId, label: `${e.name ?? e.empId}（${e.empId}）` })));
  };
  const addWorker = async () => {
    if (!wkPick) return;
    const r = await addChiefWorker(wkPick, form.empId);
    setChiefWorkers(r.items);
    setWkPick(undefined);
    setWkOptions([]);
  };
  const removeWorker = async (workerEmpId: string) => {
    const r = await deleteChiefWorker(form.empId, workerEmpId);
    setChiefWorkers(r.items);
  };
  const refreshChiefWorkers = () => getChiefWorkers(form.empId).then((r) => setChiefWorkers(r.items));

  return (
    <div>
      <h2 data-testid="employees-title">员工管理</h2>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search
          placeholder="搜索工号/姓名"
          allowClear
          style={{ width: 240 }}
          value={kw}
          onChange={(e) => setKw(e.target.value)}
        />
        {actorRole === "admin" && (
          <Button type="primary" data-testid="employee-new" onClick={openNew}>
            新增员工
          </Button>
        )}
        <span style={{ color: "#888" }}>共 {items.length} 人</span>
        <Toggles />
      </Space>
      <Space style={{ marginBottom: 16 }} wrap>
        {(actorRole === "hr" || actorRole === "admin") && (
          <Space size={6}>
            <Switch
              checked={showDepartedToChief}
              loading={visibilitySaving}
              onChange={(checked) => void toggleChiefDepartedVisibility(checked)}
              data-testid="chief-departed-visibility"
            />
            <span>值班长可见离职员工</span>
            <span style={{ color: "#aaa", fontSize: 12 }}>
              （影响 /confirm 代签与加班选人）
            </span>
          </Space>
        )}
        <Select
          allowClear
          placeholder="在职状态"
          style={{ minWidth: 140 }}
          value={employmentStatusFilter}
          onChange={setEmploymentStatusFilter}
          options={[
            { value: "active", label: "在职" },
            { value: "departed", label: "离职" },
          ]}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder="地区（多选）"
          style={{ minWidth: 220 }}
          value={regionFilters}
          disabled={!!actorRegion && (actorRole === "hr" || actorRole === "stat")}
          onChange={setRegionFilters}
          options={regions.map((r) => ({ value: r, label: r }))}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder="角色（多选）"
          style={{ minWidth: 220 }}
          value={roleFilters}
          onChange={setRoleFilters}
          options={roleOpts}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder="钉钉通知状态（多选）"
          style={{ minWidth: 220 }}
          value={pushFilters}
          onChange={setPushFilters}
          options={[{ value: true, label: "已开启" }, { value: false, label: "未开启" }]}
        />
      </Space>
      <Table
        rowKey="empId"
        dataSource={items}
        loading={loading}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        columns={(() => {
          const cols: ColumnsType<AdminEmployee> = [];
          if (prefs.id) cols.push({ title: "ID", dataIndex: "id", width: 70 });
          cols.push(
            { title: "工号", dataIndex: "empId" },
            { title: "姓名", dataIndex: "name" },
            { title: "钉钉UserId", dataIndex: "ddUserId" },
            {
              title: "在职状态",
              dataIndex: "employmentStatus",
              width: 100,
              render: (status: AdminEmployee["employmentStatus"]) =>
                status === "departed"
                  ? <Tag color="red">离职</Tag>
                  : <Tag color="green">在职</Tag>,
            },
            {
              title: "角色",
              dataIndex: "role",
              render: (r: string) => (
                <Tag color={r === "chief" ? "orange" : r === "worker" ? "blue" : "purple"}>
                  {roleLabel[r] ?? r}
                </Tag>
              ),
            },
            {
              title: "地区",
              dataIndex: "regions",
              width: 160,
              render: (values: string[] | undefined) =>
                values?.length
                  ? <Space size={[0, 4]} wrap>{values.map((value) => <Tag key={value}>{value}</Tag>)}</Space>
                  : <span style={{ color: "#aaa" }}>未配置</span>,
            },
            {
              title: "钉钉通知",
              dataIndex: "pushEnabled",
              width: 90,
              render: (v: boolean) =>
                v ? <Tag color="green">开</Tag> : <Tag>关</Tag>,
            }
          );
          if (prefs.updatedAt)
            cols.push({ title: "修改时间", dataIndex: "updatedAt", render: (v) => fmtTime(v) });
          if (prefs.updatedBy)
            cols.push({ title: "修改人", dataIndex: "updatedBy", render: (v) => v ?? "-" });
          cols.push({
            title: "操作",
            render: (_, e: AdminEmployee) => (
              <Space>
                <Button type="link" onClick={() => void openEdit(e)}>
                  {canEdit(e.role) ? "编辑" : "查看"}
                </Button>
                {actorRole === "admin" && (
                  <Popconfirm title={`删除「${e.name}」？`} onConfirm={() => remove(e.empId)}>
                    <Button type="link" danger>
                      删除
                    </Button>
                  </Popconfirm>
                )}
              </Space>
            ),
          });
          return cols;
        })()}
      />

      <Drawer
        open={open}
        onClose={() => setOpen(false)}
        width={520}
        title={isNew ? "新增员工" : !formEditable ? `员工 · ${form.name}（只读）` : `编辑员工 · ${form.name}`}
        extra={
          formEditable && (
            <Button type="primary" loading={saving} onClick={save} data-testid="employee-save">
              保存
            </Button>
          )
        }
      >
        <Space direction="vertical" size="middle" style={{ display: "flex" }}>
          <div>
            <div style={{ color: "#888", marginBottom: 4 }}>工号</div>
            <Input
              value={form.empId}
              disabled={!isNew || !formEditable}
              onChange={(e) => set({ empId: e.target.value })}
              placeholder="工号（唯一）"
            />
          </div>
          <div>
            <div style={{ color: "#888", marginBottom: 4 }}>姓名</div>
            <Input value={form.name} disabled={!formEditable} onChange={(e) => set({ name: e.target.value })} />
          </div>
          <div>
            <div style={{ color: "#888", marginBottom: 4 }}>钉钉 UserId</div>
            <Input value={form.ddUserId} disabled={actorRole !== "admin" || !formEditable} onChange={(e) => set({ ddUserId: e.target.value })} placeholder="免登 userId（缺省同工号）" />
          </div>
          <div>
            <div style={{ color: "#888", marginBottom: 4 }}>角色</div>
            <Select style={{ width: 200 }} value={form.role} disabled={!formEditable} onChange={(v) => set({ role: v })} options={editableRoleOpts} />
          </div>
          {!isNew && (
            <div>
              <div style={{ color: "#888", marginBottom: 4 }}>在职状态</div>
              <Space size={4}>
                {form.employmentStatus === "departed"
                  ? <Tag color="red">离职</Tag>
                  : <Tag color="green">在职</Tag>}
                <span style={{ color: "#aaa", fontSize: 12 }}>（由通讯录同步）</span>
              </Space>
            </div>
          )}
          {!isNew && (
            <div>
              <div style={{ color: "#888", marginBottom: 4 }}>
                钉钉通知
              </div>
              <Switch
                checked={form.pushEnabled}
                disabled={!formEditable}
                checkedChildren="开"
                unCheckedChildren="关"
                onChange={(v) => set({ pushEnabled: v })}
                data-testid="employee-push-enabled"
              />
            </div>
          )}

          <div>
            <div style={{ color: "#888", marginBottom: 4 }}>常用地区</div>
            {(form.role === "hr" || form.role === "stat") && form.region.length > 1 && (
              <Alert
                type="warning"
                showIcon
                message="历史数据包含多个地区，请明确保留一个地区后再保存"
                style={{ marginBottom: 8 }}
              />
            )}
            <Select
              mode="multiple"
              style={{ width: "100%" }}
              value={form.region}
              disabled={!formEditable}
              onChange={(v) => set({ region: v })}
              options={regions.map((r) => ({ value: r, label: r }))}
            />
          </div>
          <div>
            <div style={{ color: "#888", marginBottom: 4 }}>常用工作点</div>
            <Select
              mode="multiple"
              style={{ width: "100%" }}
              value={form.furnace}
              disabled={!formEditable}
              onChange={(v) => set({ furnace: v })}
              options={furnaces.map((f) => ({ value: f, label: f }))}
            />
          </div>
          <div>
            <div style={{ color: "#888", marginBottom: 4 }}>常用职位</div>
            <Select
              mode="multiple"
              style={{ width: "100%" }}
              value={form.post}
              disabled={!formEditable}
              onChange={(v) => set({ post: v })}
              options={posts.map((p) => ({ value: p, label: p }))}
            />
          </div>

          {form.role === "chief" && !isNew && !formEditable && (
            <div style={{ borderTop: "1px solid #f0f0f0", paddingTop: 12 }}>
              <div style={{ fontWeight: 600, marginBottom: 8 }}>值班长关联操作工（常用员工/代签名单）</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {chiefWorkers.map((w) => (
                  <Tag key={w.empId} color="blue">
                    {w.name ?? w.empId}（{w.empId}）
                  </Tag>
                ))}
                {chiefWorkers.length === 0 && <span style={{ color: "#bbb" }}>暂无</span>}
              </div>
            </div>
          )}
          {form.role === "chief" && !isNew && formEditable && (
            <div style={{ borderTop: "1px solid #f0f0f0", paddingTop: 12 }}>
              <div style={{ fontWeight: 600, marginBottom: 8 }}>值班长关联操作工（常用员工/代签名单）</div>
              <Space style={{ marginBottom: 8 }}>
                <AutoComplete
                  style={{ width: 280 }}
                  options={wkOptions}
                  value={wkPick}
                  onChange={(v) => setWkPick(v)}
                  onSearch={searchWorkers}
                  onFocus={refreshChiefWorkers}
                  placeholder="搜索工号/姓名添加"
                  filterOption={false}
                />
                <Button disabled={!wkPick} onClick={addWorker}>
                  添加
                </Button>
              </Space>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {chiefWorkers.map((w) => (
                  <Tag key={w.empId} closable color="blue" onClose={(ev) => { ev.preventDefault(); void removeWorker(w.empId); }}>
                    {w.name ?? w.empId}（{w.empId}）
                  </Tag>
                ))}
                {chiefWorkers.length === 0 && <span style={{ color: "#bbb" }}>暂无</span>}
              </div>
            </div>
          )}
          {form.role === "chief" && isNew && (
            <div style={{ color: "#bbb", fontSize: 12 }}>保存后可在编辑里维护「值班长关联操作工」。</div>
          )}
          {!isNew && belongChiefs.length > 0 && (
            <div style={{ borderTop: "1px solid #f0f0f0", paddingTop: 12 }}>
              <div style={{ fontWeight: 600, marginBottom: 8 }}>所属值班长（被以下值班长关联为常用员工/代签名单）</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {belongChiefs.map((c) => (
                  <Tag key={c.empId} color="green">
                    {c.name ?? c.empId}（{c.empId}）
                  </Tag>
                ))}
              </div>
            </div>
          )}
        </Space>
      </Drawer>
    </div>
  );
}
