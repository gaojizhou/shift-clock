import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Card,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  message,
} from "antd";
import type { TabsProps } from "antd";
import {
  addPostCatalog,
  applyQuotaTemplate,
  getEmployees,
  getFurnaces,
  getFurnaceTypes,
  getMe,
  getPostCatalog,
  getQuotaTemplates,
  getRegions,
  getSelectablePosts,
  overwriteFurnaceQuota,
  putQuotaTemplates,
  updateFurnaceConfig,
  SQUADS,
  type EmployeeItem,
  type FurnaceItem,
  type QuotaTemplateItem,
} from "@fdc/api-client";
import QuotaItemsEditor, { type QuotaRow } from "../components/QuotaItemsEditor";
import FurnaceQrExport from "../components/FurnaceQrExport";
import { usePageCache } from "../pageCache";

const ALL = "全部";

const REGION_COLOR: Record<string, string> = { 示例厂区A: "blue", 示例厂区B: "green" };
// 设备类型不再前端硬编码：列表来自字典接口，颜色按名称哈希从池中分配
const TYPE_COLOR: Record<string, string> = {};
const FALLBACK = ["purple", "volcano", "geekblue", "lime", "orange", "red"];
const colorOf = (map: Record<string, string>, key: string, pool: string[]) =>
  map[key] ?? pool[[...key].reduce((a, c) => a + c.charCodeAt(0), 0) % pool.length];

// 工作点 quota_rule 明细 → 编辑行（按岗位聚合，识别三班是否联动）
// visibleByPost：岗位 → 可见角色（来自 selectable_post），缺省按分班→worker、不分班→chief
function rulesToRows(
  rules: FurnaceItem["quotaRules"],
  visibleByPost: Map<string, string[]>
): QuotaRow[] {
  const byPost = new Map<string, FurnaceItem["quotaRules"]>();
  for (const r of rules) {
    const list = byPost.get(r.post) ?? [];
    list.push(r);
    byPost.set(r.post, list);
  }
  const rolesFor = (post: string, squadless: boolean) =>
    visibleByPost.get(post) ?? (squadless ? ["chief"] : ["worker"]);
  const rows: QuotaRow[] = [];
  for (const [post, list] of byPost) {
    const squadless = list.some((r) => r.squad === null);
    if (squadless) {
      const cap = list[0].capacity;
      rows.push({
        post,
        squadless: true,
        linked: true,
        caps: [cap, cap, cap],
        visibleRoles: rolesFor(post, true),
      });
      continue;
    }
    const capBy = (s: string) => list.find((r) => r.squad === s)?.capacity;
    const c1 = capBy("1班") ?? list[0].capacity;
    const c2 = capBy("2班") ?? c1;
    const c3 = capBy("3班") ?? c1;
    rows.push({
      post,
      squadless: false,
      linked: c1 === c2 && c2 === c3,
      caps: [c1, c2, c3],
      visibleRoles: rolesFor(post, false),
    });
  }
  return rows;
}

function rowsToRules(rows: QuotaRow[]): { squad: string | null; post: string; capacity: number }[] {
  const out: { squad: string | null; post: string; capacity: number }[] = [];
  for (const row of rows) {
    if (!row.post.trim()) continue;
    if (row.squadless) {
      out.push({ squad: null, post: row.post, capacity: row.caps[0] });
    } else {
      const caps = row.linked ? [row.caps[0], row.caps[0], row.caps[0]] : row.caps;
      SQUADS.forEach((s, i) => out.push({ squad: s, post: row.post, capacity: caps[i] }));
    }
  }
  return out;
}

// 编辑行 → 工作点级「可见角色」覆盖：{post, roles}，仅含选了角色的岗位
const rowsToVisiblePosts = (rows: QuotaRow[]) =>
  rows
    .filter((r) => r.post.trim() && r.visibleRoles.length > 0)
    .map((r) => ({ post: r.post, roles: r.visibleRoles }));

// 模板项 ↔ 编辑行
function itemsToRows(items: QuotaTemplateItem[]): QuotaRow[] {
  return items.map((i) => {
    const c1 = i.capacity;
    const c2 = i.capacity2 ?? c1;
    const c3 = i.capacity3 ?? c1;
    return {
      post: i.post,
      squadless: i.squadless,
      linked: c1 === c2 && c2 === c3,
      caps: [c1, c2, c3] as [number, number, number],
      visibleRoles: i.visibleRoles ?? (i.squadless ? ["chief"] : ["worker"]),
    };
  });
}
function rowsToItems(rows: QuotaRow[]): QuotaTemplateItem[] {
  return rows
    .filter((r) => r.post.trim())
    .map((r) => ({
      post: r.post,
      capacity: r.caps[0],
      capacity2: r.squadless ? r.caps[0] : r.linked ? r.caps[0] : r.caps[1],
      capacity3: r.squadless ? r.caps[0] : r.linked ? r.caps[0] : r.caps[2],
      squadless: r.squadless,
      visibleRoles: r.visibleRoles,
    }));
}

export default function FurnacesPage({ active = true }: { active?: boolean }) {
  const [items, setItems] = useState<FurnaceItem[]>([]);
  const [posts, setPosts] = useState<string[]>([]);
  const [dictTypes, setDictTypes] = useState<string[]>([]);
  const [dictRegions, setDictRegions] = useState<string[]>([]);
  const [viewer, setViewer] = useState<{ role: string; region?: string } | null>(null);
  const { invalidate } = usePageCache();
  const previousActive = useRef(active);
  const [listLoadFailed, setListLoadFailed] = useState(false);
  const reload = useCallback(async () => {
    try {
      const response = await getFurnaces();
      setItems(response.items);
      setListLoadFailed(false);
      return response.items;
    } catch (e) {
      setListLoadFailed(true);
      message.error((e as { message?: string }).message || "工作点列表加载失败");
      return [];
    }
  }, []);
  const loadDictionaries = useCallback(async () => {
    await Promise.all([
      getPostCatalog().then((r) => setPosts(r.items)).catch(() => undefined),
      getRegions().then((r) => setDictRegions(r.items.map((item) => item.name))).catch(() => undefined),
      getMe().then((me) => setViewer(me)).catch(() => undefined),
      getFurnaceTypes().then((r) => setDictTypes(r.items.map((t) => t.name))).catch(() => undefined),
    ]);
  }, []);
  useEffect(() => {
    void reload();
    void loadDictionaries();
  }, [reload, loadDictionaries]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) {
      invalidate("/furnaces");
      void reload();
      void loadDictionaries();
    }
  }, [active, invalidate, reload, loadDictionaries]);

  const addPost = async (name: string) => {
    const r = await addPostCatalog(name);
    setPosts(r.items);
  };

  const regions = useMemo(() => {
    if (viewer && viewer.role !== "admin") {
      return viewer.region ? [viewer.region] : [...new Set(items.map((i) => i.region))];
    }
    return dictRegions.length ? dictRegions : [...new Set(items.map((i) => i.region))];
  }, [dictRegions, items, viewer]);
  // 设备类型来自字典；字典未加载到时回退为工作点数据里出现过的设备类型
  const types = useMemo(
    () => (dictTypes.length ? dictTypes : [...new Set(items.map((i) => i.furnaceType))]),
    [dictTypes, items]
  );

  return (
    <div>
      <h2 data-testid="furnaces-title">工作点与编制配置</h2>
      <Tabs
        defaultActiveKey="byFurnace"
        items={[
          {
            key: "byFurnace",
            label: "工作点编制",
            children: (
              <ByFurnacePanel active={active} items={items} regions={regions} types={types} posts={posts} onAddPost={addPost} onReload={reload} />
            ),
          },
          {
            key: "templates",
            label: "设备类型编制模板（批量）",
            children: <TemplatesPanel active={active} types={types} regions={regions} items={items} posts={posts} onAddPost={addPost} onReload={reload} />,
          },
          // 岗位字典已并入「基础字典」页维护，此处不再重复提供
        ].filter(Boolean) as TabsProps["items"]}
      />
    </div>
  );
}

/** 子页面一（默认）：具体每个工作点的编制编辑。 */
function ByFurnacePanel({
  active,
  items,
  regions,
  types,
  posts,
  onAddPost,
  onReload,
}: {
  active: boolean;
  items: FurnaceItem[];
  regions: string[];
  types: string[];
  posts: string[];
  onAddPost: (name: string) => Promise<void>;
  onReload: () => Promise<FurnaceItem[]>;
}) {
  const { invalidate } = usePageCache();
  const [region, setRegion] = useState<string>(ALL);
  const [furnaceType, setFurnaceType] = useState<string>(ALL);
  const [diff, setDiff] = useState<string>(ALL);

  // 二维码导出：要导出的工作点列表 + 弹窗标题（空数组=关闭）
  const [qrFurnaces, setQrFurnaces] = useState<FurnaceItem[] | null>(null);
  const [qrTitle, setQrTitle] = useState("导出工作点二维码");
  // 多选导出：选中的行 key
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);

  const [edit, setEdit] = useState<FurnaceItem | null>(null);
  const [editRows, setEditRows] = useState<QuotaRow[]>([]);
  const [reference, setReference] = useState<Record<string, QuotaRow> | undefined>();
  const [saving, setSaving] = useState(false);

  // 值班长/设备类型配置弹窗
  const [cfg, setCfg] = useState<FurnaceItem | null>(null);
  const [cfgChiefs, setCfgChiefs] = useState<string[]>([]);
  const [cfgType, setCfgType] = useState<string>("");
  const [cfgRegion, setCfgRegion] = useState<string>("");
  const [cfgFurnaceNo, setCfgFurnaceNo] = useState<string>("");
  const [cfgSaving, setCfgSaving] = useState(false);
  const [employees, setEmployees] = useState<EmployeeItem[]>([]);
  useEffect(() => {
    if (active) return;
    setCfg(null);
    setEdit(null);
    setQrFurnaces(null);
  }, [active]);
  useEffect(() => {
    // 值班长候选改回 getEmployees()：返回全量花名册（含 role），不做地区裁剪，
    // 避免无地区/跨地区员工被整条剔除。
    getEmployees()
      .then((r) => setEmployees(r.items))
      .catch((e) => message.error((e as { message?: string }).message || "员工列表加载失败"));
  }, [active]);

  const openCfg = (row: FurnaceItem) => {
    setCfg(row);
    setCfgChiefs(
      row.chiefs && row.chiefs.length > 0
        ? row.chiefs.map((c) => c.empId)
        : row.chiefUserId
          ? [row.chiefUserId]
          : []
    );
    setCfgType(row.furnaceType || "");
    setCfgRegion(row.region || "");
    setCfgFurnaceNo(row.furnaceNo || "");
  };

  const saveCfg = async () => {
    if (!cfg) return;
    setCfgSaving(true);
    try {
      await updateFurnaceConfig(cfg.id, {
        chiefIds: cfgChiefs,
        furnaceType: cfgType || undefined,
        region: cfgRegion || undefined,
        furnaceNo: cfgFurnaceNo.trim() || undefined,
      });
      message.success("已保存");
      await onReload();
      setCfg(null);
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "保存失败");
    } finally {
      setCfgSaving(false);
    }
  };

  const filtered = useMemo(
    () =>
      items.filter(
        (f) =>
          (region === ALL || f.region === region) &&
          (furnaceType === ALL || f.furnaceType === furnaceType) &&
          (diff === ALL ||
            (diff === "已改动" && f.customized) ||
            (diff === "与模板一致" && f.hasTemplate && !f.customized))
      ),
    [items, region, furnaceType, diff]
  );

  const openEdit = async (f: FurnaceItem) => {
    // 编辑是全量覆盖写；打开时必须真正回源拿最新 quotaRules：
    // /furnaces 响应会被会话缓存拦截，先 invalidate 再 reload；
    // 任一请求失败要给出提示且不打开弹窗，避免基于旧快照/残留行做覆盖写。
    try {
      invalidate("/furnaces");
      const latestItems = await onReload();
      const current = latestItems.find((item) => item.id === f.id) ?? f;
      const visibleByPost = await getSelectablePosts(current.region, current.furnaceNo).then(
        (r) => new Map((r.visible ?? []).map((v) => [v.post, v.roles]))
      );
      const rows = rulesToRows(current.quotaRules, visibleByPost);
      // 先在弹窗外备齐数据，再清残留并打开，避免短暂展示上一次编辑的行。
      let nextReference: Record<string, QuotaRow> | undefined;
      if (current.customized) {
        // 被改动过：左侧淡色对照该设备类型模板数据
        const tpl = await getQuotaTemplates(current.furnaceType, current.region).then((r) => itemsToRows(r.items));
        nextReference = Object.fromEntries(tpl.map((t) => [t.post, t]));
      }
      setReference(nextReference);
      setEditRows(rows);
      setEdit(current);
    } catch (e) {
      setEdit(null);
      message.error((e as { data?: { detail?: string } }).data?.detail || "打开编制编辑失败，请稍后重试");
    }
  };

  const save = async () => {
    if (!edit) return;
    setSaving(true);
    try {
      await overwriteFurnaceQuota(edit.id, rowsToRules(editRows), rowsToVisiblePosts(editRows));
      message.success("本工作点编制已保存");
      await onReload();
      setEdit(null);
    } catch {
      message.error("保存失败");
    } finally {
      setSaving(false);
    }
  };

  const quotaSummary = (row: FurnaceItem) => {
    const rules = row.quotaRules ?? [];
    const squads = new Set(rules.filter((q) => q.squad && !q.squadless).map((q) => q.squad));
    const ps = new Set(rules.map((q) => q.post));
    const totalCap = rules.reduce((s, q) => s + (q.capacity ?? 0), 0);
    return `${squads.size > 0 ? `${squads.size}班` : "无班组"} · ${ps.size}岗位 · 编制${totalCap}人`;
  };

  return (
    <div>
      <Space direction="vertical" size="middle" style={{ marginBottom: 16, display: "flex" }}>
        <Space wrap>
          <span style={{ color: "#888" }}>地区：</span>
          <Segmented value={region} onChange={(v) => setRegion(v as string)} options={[ALL, ...regions]} />
        </Space>
        <Space wrap>
          <span style={{ color: "#888" }}>设备类型：</span>
          <Segmented value={furnaceType} onChange={(v) => setFurnaceType(v as string)} options={[ALL, ...types]} />
        </Space>
        <Space wrap>
          <span style={{ color: "#888" }}>与模板：</span>
          <Segmented value={diff} onChange={(v) => setDiff(v as string)} options={[ALL, "已改动", "与模板一致"]} />
        </Space>
        <Space wrap>
          <span style={{ color: "#888" }}>二维码：</span>
          <Button
            data-testid="qr-export-all"
            onClick={() => {
              setQrTitle("全量导出工作点二维码");
              setQrFurnaces(items);
            }}
          >
            全量导出二维码（{items.length}）
          </Button>
          <Button
            data-testid="qr-export-selected"
            disabled={selectedKeys.length === 0}
            onClick={() => {
              const sel = filtered.filter((f) => selectedKeys.includes(`${f.region}-${f.furnaceNo}`));
              setQrTitle(`多选导出工作点二维码（${sel.length}）`);
              setQrFurnaces(sel);
            }}
          >
            多选导出二维码（{selectedKeys.length}）
          </Button>
        </Space>
      </Space>
      <Table
        rowKey={(r) => `${r.region}-${r.furnaceNo}`}
        dataSource={filtered}
        rowSelection={{
          selectedRowKeys: selectedKeys,
          onChange: (keys) => setSelectedKeys(keys),
        }}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        columns={[
          { title: "地区", dataIndex: "region", render: (r: string) => <Tag color={colorOf(REGION_COLOR, r, FALLBACK)}>{r}</Tag> },
          { title: "工作点", dataIndex: "furnaceNo", render: (no: string, row) => <Tag color={colorOf(TYPE_COLOR, row.furnaceType, FALLBACK)}>{no}</Tag> },
          { title: "设备类型", dataIndex: "furnaceType", render: (t: string) => <Tag color={colorOf(TYPE_COLOR, t, FALLBACK)}>{t}</Tag> },
          {
            title: "值班长",
            render: (_, row) => {
              const chiefs =
                row.chiefs && row.chiefs.length > 0
                  ? row.chiefs
                  : row.chiefUserId
                    ? [{ empId: row.chiefUserId, name: row.chiefName }]
                    : [];
              return chiefs.length > 0 ? (
                <span style={{ cursor: "pointer" }} onClick={() => openCfg(row)}>
                  {chiefs.map((c) => (
                    <Tag key={c.empId} color="orange">
                      {c.name ?? c.empId}（{c.empId}）
                    </Tag>
                  ))}
                </span>
              ) : (
                <Button type="link" size="small" style={{ padding: 0 }} onClick={() => openCfg(row)}>
                  绑定值班长
                </Button>
              );
            },
          },
          {
            title: "与模板",
            dataIndex: "customized",
            render: (_, row) =>
              !row.hasTemplate ? (
                <Tag>无模板</Tag>
              ) : row.customized ? (
                <Tag color="red" data-testid={`diff-${row.furnaceNo}`}>已改动</Tag>
              ) : (
                <Tag color="green">与模板一致</Tag>
              ),
          },
          { title: "编制摘要", render: (_, row) => quotaSummary(row) },
          {
            title: "操作",
            render: (_, row) => (
              <>
                <Button type="link" data-testid={`edit-quota-${row.furnaceNo}`} onClick={() => void openEdit(row)}>
                  编辑当前工作点编制
                </Button>
                <Button type="link" data-testid={`edit-config-${row.furnaceNo}`} onClick={() => openCfg(row)}>
                  值班长/设备类型
                </Button>
                <Button
                  type="link"
                  data-testid={`qr-export-${row.furnaceNo}`}
                  onClick={() => {
                    setQrTitle(`导出二维码 · ${row.region} ${row.furnaceNo}`);
                    setQrFurnaces([row]);
                  }}
                >
                  导出二维码
                </Button>
              </>
            ),
          },
        ]}
      />
      <Modal
        open={!!cfg}
        onCancel={() => setCfg(null)}
        title={`工作点配置 · ${cfg?.region} ${cfg?.furnaceNo}`}
        okText="保存"
        confirmLoading={cfgSaving}
        onOk={saveCfg}
        destroyOnClose
      >
        <div style={{ display: "flex", gap: 12, marginBottom: 16 }}>
          <div style={{ flex: 1 }}>
            <div style={{ marginBottom: 6, color: "#666" }}>地区</div>
            <Select
              data-testid="cfg-region"
              style={{ width: "100%" }}
              value={cfgRegion || undefined}
              onChange={(v) => setCfgRegion(v)}
              options={regions.map((rg) => ({ value: rg, label: rg }))}
              placeholder="选择地区"
            />
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ marginBottom: 6, color: "#666" }}>工作点编号</div>
            <Input
              data-testid="cfg-furnace-no"
              value={cfgFurnaceNo}
              onChange={(e) => setCfgFurnaceNo(e.target.value)}
              placeholder="如 101号工作点"
            />
          </div>
        </div>
        <div style={{ marginBottom: 16 }}>
          <div style={{ marginBottom: 6, color: "#666" }}>设备类型（来自基础字典）</div>
          <Select
            data-testid="cfg-furnace-type"
            style={{ width: "100%" }}
            value={cfgType || undefined}
            onChange={(v) => setCfgType(v)}
            options={types.map((t) => ({ value: t, label: t }))}
            placeholder="选择设备类型"
          />
        </div>
        <div>
          <div style={{ marginBottom: 6, color: "#666" }}>
            值班长（可多选；第一位为主值班长。新绑定的操作工将升为值班长角色，解绑后不再担任任何工作点值班长的降回操作工）
          </div>
          <Select
            data-testid="cfg-chiefs"
            mode="multiple"
            showSearch
            optionFilterProp="label"
            style={{ width: "100%" }}
            value={cfgChiefs}
            onChange={(v) => setCfgChiefs(v)}
            options={employees.map((e) => ({
              value: e.empId,
              label: `${e.name}（${e.empId}）${e.role === "chief" ? " · 值班长" : ""}`,
            }))}
            placeholder="搜索姓名或工号"
          />
        </div>
      </Modal>
      <Modal
        open={!!edit}
        onCancel={() => {
          setEdit(null);
          setEditRows([]);
          setReference(undefined);
        }}
        title={`编辑当前工作点编制 · ${edit?.region} ${edit?.furnaceNo}`}
        width="96vw"
        style={{ top: 16, maxWidth: 1400 }}
        styles={{ body: { maxHeight: "calc(100vh - 200px)", overflowY: "auto" } }}
        okText="保存"
        confirmLoading={saving}
        onOk={save}
        destroyOnClose
      >
        <QuotaItemsEditor value={editRows} onChange={setEditRows} idPrefix="furnace-" posts={posts} onAddPost={onAddPost} />
        {reference && (
          <div
            style={{
              marginTop: 20,
              padding: 12,
              background: "#fafafa",
              border: "1px dashed #d9d9d9",
              borderRadius: 8,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
              <span style={{ fontWeight: 600, color: "#fa8c16" }}>
                该工作点已被改动 · 对应设备类型模板数据（对照）
              </span>
              <Popconfirm
                title="套用该设备类型模板覆盖当前编辑？"
                description="将用模板数据替换上方编辑内容（保存后才生效）"
                okText="覆盖"
                cancelText="取消"
                onConfirm={() =>
                  setEditRows(Object.values(reference).map((r) => ({ ...r, caps: [...r.caps] as [number, number, number] })))
                }
              >
                <Button size="small" data-testid="apply-template-to-editor">
                  套用模板覆盖当前
                </Button>
              </Popconfirm>
            </div>
            <div style={{ color: "#999", fontSize: 13, lineHeight: 1.9 }}>
              {Object.values(reference).map((r) => (
                <div key={r.post}>
                  {r.post}：
                  {r.squadless
                    ? `不分班 ${r.caps[0]} 人`
                    : `分班 ${r.caps[0]}/${r.caps[1]}/${r.caps[2]} 人`}
                  {r.visibleRoles.length > 0 ? ` · 可见:${r.visibleRoles.join("/")}` : ""}
                </div>
              ))}
            </div>
          </div>
        )}
      </Modal>

      <FurnaceQrExport
        open={qrFurnaces !== null}
        furnaces={qrFurnaces ?? []}
        title={qrTitle}
        onClose={() => setQrFurnaces(null)}
      />
    </div>
  );
}

/** 子页面二：设备类型编制模板（批量）——设备类型少，三种全部展开同屏编辑。 */
function TemplatesPanel({
  active = true,
  types,
  regions,
  items,
  posts,
  onAddPost,
  onReload,
}: {
  active?: boolean;
  types: string[];
  regions: string[];
  items: FurnaceItem[];
  posts: string[];
  onAddPost: (name: string) => Promise<void>;
  onReload: () => Promise<FurnaceItem[]>;
}) {
  const [tplByType, setTplByType] = useState<Record<string, QuotaRow[]>>({});
  const [busy, setBusy] = useState<string>("");
  const [region, setRegion] = useState("");
  // 加载失败时编辑器展示为空，若不区分「加载中/失败/空模板」，保存会把
  // 空数组覆盖式写回后端（upsert 先删后写），直接清空该地区该设备类型模板。
  const [tplLoading, setTplLoading] = useState(false);
  const [tplLoadFailed, setTplLoadFailed] = useState(false);
  const localBaselineRef = useRef<Record<string, QuotaRow[]>>({});

  useEffect(() => {
    if (!regions.length) {
      setRegion("");
      return;
    }
    setRegion((current) => regions.includes(current) ? current : regions[0]);
  }, [regions]);

  const previousActive = useRef(active);
  const loadTemplates = useCallback((targetRegion: string) => {
    setTplLoadFailed(false);
    if (!targetRegion) return;
    setTplLoading(true);
    let cancelled = false;
    void Promise.all(types.map(async (type) => {
      const response = await getQuotaTemplates(type, targetRegion);
      return [type, itemsToRows(response.items)] as const;
    })).then((entries) => {
      if (!cancelled) {
        const next = Object.fromEntries(entries);
        setTplByType(next);
        localBaselineRef.current = next;
      }
    }).catch(() => {
      if (!cancelled) {
        setTplLoadFailed(true);
        message.error("模板加载失败");
      }
    }).finally(() => {
      if (!cancelled) setTplLoading(false);
    });
    return () => { cancelled = true; };
  }, [types]);

  useEffect(() => {
    setTplByType({});
    return loadTemplates(region);
  }, [region, loadTemplates]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive && region) {
      loadTemplates(region);
    }
  }, [active, region, loadTemplates]);

  const countOf = (t: string) => items.filter(
    (f) => f.furnaceType === t && f.region === region
  ).length;

  const saveTpl = async (t: string) => {
    // 加载中/加载失败时编辑器内容是空的或不可信，禁止覆盖式保存，
    // 否则会把空模板写回后端（upsert 先删后写），清空该地区该设备类型模板。
    if (tplLoading || tplLoadFailed) return;
    setBusy(`save-${t}`);
    try {
      const latest = await getQuotaTemplates(t, region);
      const latestRows = itemsToRows(latest.items);
      const localRows = tplByType[t] ?? [];
      if (JSON.stringify(latestRows) !== JSON.stringify(localBaselineRef.current[t] ?? [])) {
        const ok = window.confirm(`「${t}」模板已被其他人更新，继续保存将覆盖服务器版本。确定？`);
        if (!ok) {
          setTplByType((prev) => ({ ...prev, [t]: latestRows }));
          localBaselineRef.current[t] = latestRows;
          return;
        }
      }
      const r = await putQuotaTemplates(t, rowsToItems(localRows), region);
      const saved = itemsToRows(r.items);
      setTplByType((prev) => ({ ...prev, [t]: saved }));
      localBaselineRef.current[t] = saved;
      message.success(`「${t}」模板已保存`);
    } catch {
      message.error("模板保存失败");
    } finally {
      setBusy("");
    }
  };

  const applyTpl = async (t: string) => {
    setBusy(`apply-${t}`);
    try {
      const r = await applyQuotaTemplate(t, undefined, region);
      message.success(`已应用到 ${r.applied.length} 个工作点`);
      await onReload();
    } catch {
      message.error("批量应用失败");
    } finally {
      setBusy("");
    }
  };

  const templateDisabled = !region || tplLoading || tplLoadFailed;

  return (
    <div data-testid="quota-template-card">
      <p style={{ color: "#888" }}>
        模板按地区隔离。保存仅写当前地区模板；批量应用为覆盖式，只会重写当前地区该设备类型工作点的编制与扫码可选。
      </p>
      {tplLoadFailed && (
        <p style={{ color: "#cf1322" }}>
          模板加载失败：当前展示为空不代表模板为空，已禁用保存/批量应用，请重试以免误清空。
        </p>
      )}
      <Space style={{ marginBottom: 16 }}>
        <span>模板地区：</span>
        <Select
          value={region || undefined}
          placeholder="请选择地区"
          style={{ minWidth: 180 }}
          options={regions.map((value) => ({ value, label: value }))}
          onChange={setRegion}
        />
      </Space>
      <Space direction="vertical" size="large" style={{ display: "flex" }}>
        {types.map((t) => {
          const emptyTemplate = !tplLoading && !tplLoadFailed && (tplByType[t] ?? []).length === 0;
          const saveButton = (
            <Button disabled={templateDisabled} loading={busy === `save-${t}`} data-testid={`template-save-${t}`} onClick={emptyTemplate ? undefined : () => saveTpl(t)}>
              保存模板
            </Button>
          );
          return (
            <Card
              key={t}
              size="small"
              title={
                <Space>
                  <Tag color={colorOf(TYPE_COLOR, t, FALLBACK)}>{t}</Tag>
                  <span style={{ color: "#888", fontWeight: 400 }}>{countOf(t)} 个工作点</span>
                </Space>
              }
              extra={
                <Space>
                  {emptyTemplate ? (
                    // 空模板保存 = 清空该地区该设备类型全部模板行，显式二次确认防误操作
                    <Popconfirm title="当前模板为空，保存将清空该地区该设备类型的全部模板，确定？" onConfirm={() => saveTpl(t)}>
                      {saveButton}
                    </Popconfirm>
                  ) : saveButton}
                  <Popconfirm title={`覆盖式应用到当前地区「${t}」的全部工作点（以后端实际台数为准）？`} onConfirm={() => applyTpl(t)}>
                    <Button type="primary" disabled={templateDisabled || countOf(t) === 0} loading={busy === `apply-${t}`} data-testid={`template-apply-${t}`}>
                      批量应用
                    </Button>
                  </Popconfirm>
                </Space>
              }
            >
              <QuotaItemsEditor
                value={tplByType[t] ?? []}
                onChange={(next) => setTplByType((prev) => ({ ...prev, [t]: next }))}
                idPrefix={`tpl-${t}-`}
                posts={posts}
                onAddPost={onAddPost}
              />
            </Card>
          );
        })}
      </Space>
    </div>
  );
}
