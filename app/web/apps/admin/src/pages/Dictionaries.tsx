import { useEffect, useRef, useState } from "react";
import { Button, Input, InputNumber, Popconfirm, Select, Space, Table, Tabs, Tag, message } from "antd";
import { createDict, deleteDict, getDict, updateDict, type DictItem } from "@fdc/api-client";
import { useListCols, fmtTime } from "../useListCols";
import { usePageCache } from "../pageCache";

const KINDS = [
  { key: "role", label: "角色", codeEditable: true },
  { key: "position", label: "职位", codeEditable: false },
  { key: "region", label: "地区", codeEditable: false },
  { key: "squad", label: "班组", codeEditable: false },
  { key: "furnace_type", label: "设备类型", codeEditable: false },
];

function DictTab({ kind, codeEditable, refreshTick = 0 }: { kind: string; codeEditable: boolean; refreshTick?: number }) {
  const [items, setItems] = useState<DictItem[]>([]);
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const { prefs, Toggles } = useListCols(`dict-${kind}`, { id: true });
  const [loading, setLoading] = useState(false);
  const reload = () => {
    setLoading(true);
    return getDict(kind)
      .then((r) => setItems(r.items))
      .catch((e) => {
        message.error((e as { data?: { detail?: string } }).data?.detail || "字典加载失败");
      })
      .finally(() => setLoading(false));
  };
  useEffect(() => {
    reload();
    // refreshTick 由页面重激活时抬升：常驻挂载的缓存页切回时必须重新拉取，
    // 否则会长时间展示其他管理员已修改/停用的旧字典。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, refreshTick]);

  const add = async () => {
    if (!name.trim()) return;
    try {
      await createDict(kind, { name: name.trim(), code: codeEditable ? code.trim() || undefined : undefined });
      setName("");
      setCode("");
      reload();
      message.success("已新增");
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "新增失败");
    }
  };
  const rename = async (it: DictItem, v: string) => {
    try {
      await updateDict(kind, it.code, { name: v });
      await reload();
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "重命名失败");
      await reload();
    }
  };
  const reorder = async (it: DictItem, v: number) => {
    try {
      await updateDict(kind, it.code, { sort: v });
      await reload();
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "排序更新失败");
      await reload();
    }
  };
  const remove = async (it: DictItem) => {
    // 删被引用项后端返回 409，需 try/catch 给友好提示，避免异常冒泡为 pageerror
    try {
      await deleteDict(kind, it.code);
      message.success("已删除");
      reload();
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "删除失败（可能仍被引用）");
    }
  };

  const cols: any[] = [];
  if (prefs.id) cols.push({ title: "ID", dataIndex: "code", width: 120 });
  cols.push(
    {
      title: "名称",
      dataIndex: "name",
      render: (v: string, r: DictItem) => (
        // key 带值：refreshTick 重取到他人修改后的新值时强制重建输入框，
        // 否则 defaultValue 被复用忽略、失焦还会把残留旧值写回覆盖他人修改。
        <Input key={`${r.code}:${v}`} defaultValue={v} onBlur={(e) => e.target.value !== v && void rename(r, e.target.value)} style={{ width: 200 }} />
      ),
    },
    {
      title: "排序",
      dataIndex: "sort",
      width: 120,
      render: (v: number, r: DictItem) => (
        <InputNumber key={`${r.code}:${v}`} defaultValue={v} onBlur={(e) => Number(e.target.value) !== v && void reorder(r, Number(e.target.value))} />
      ),
    }
  );
  if (prefs.updatedAt) cols.push({ title: "修改时间", dataIndex: "updatedAt", render: (v: string) => fmtTime(v) });
  if (prefs.updatedBy) cols.push({ title: "修改人", dataIndex: "updatedBy", width: 120, render: (v: string) => v ?? "-" });
  cols.push({
    title: "操作",
    width: 100,
    render: (_: unknown, r: DictItem) => (
      <Popconfirm title={`删除「${r.name}」？`} onConfirm={() => remove(r)}>
        <Button type="link" danger>
          删除
        </Button>
      </Popconfirm>
    ),
  });

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        {codeEditable && (
          <Input placeholder="code（如 worker）" value={code} onChange={(e) => setCode(e.target.value)} style={{ width: 160 }} />
        )}
        <Input placeholder="名称" value={name} onChange={(e) => setName(e.target.value)} style={{ width: 200 }} onPressEnter={add} />
        <Button type="primary" onClick={add}>
          新增
        </Button>
        <Toggles />
      </Space>
      <Table rowKey="code" dataSource={items} loading={loading} pagination={false} columns={cols} />
    </div>
  );
}

function FurnaceTab({ refreshTick = 0 }: { refreshTick?: number }) {
  const [items, setItems] = useState<DictItem[]>([]);
  const [regions, setRegions] = useState<DictItem[]>([]);
  const [ftypes, setFtypes] = useState<DictItem[]>([]);
  const [furnaceNo, setFurnaceNo] = useState("");
  const [regionCode, setRegionCode] = useState<string | undefined>();
  const [furnaceType, setFurnaceType] = useState("");
  const [loading, setLoading] = useState(false);
  const reload = () => {
    setLoading(true);
    return getDict("furnace")
      .then((r) => setItems(r.items))
      .finally(() => setLoading(false));
  };
  useEffect(() => {
    reload();
    getDict("region").then((r) => setRegions(r.items));
    getDict("furnace_type").then((r) => setFtypes(r.items));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshTick]);

  const add = async () => {
    if (!furnaceNo.trim() || !regionCode) {
      message.warning("请选择地区并填写工作点编号");
      return;
    }
    try {
      await createDict("furnace", {
        name: furnaceNo.trim(),
        regionCode,
        furnaceType: furnaceType.trim() || undefined,
      });
      setFurnaceNo("");
      setFurnaceType("");
      reload();
      message.success("已新增（FUR 编码自动分配）");
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "新增失败");
    }
  };
  const setStatus = async (it: DictItem, status: string) => {
    try {
      await updateDict("furnace", it.code, { status });
      reload();
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "操作失败");
    }
  };
  const retype = async (it: DictItem, v: string) => {
    try {
      await updateDict("furnace", it.code, { furnaceType: v });
      reload();
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "操作失败");
    }
  };

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          placeholder="地区"
          value={regionCode}
          onChange={setRegionCode}
          style={{ width: 180 }}
          options={regions.map((r) => ({ value: r.code, label: `${r.name}（${r.code}）` }))}
        />
        <Input placeholder="工作点编号（如 5号工作点）" value={furnaceNo} onChange={(e) => setFurnaceNo(e.target.value)} style={{ width: 160 }} onPressEnter={add} />
        <Select
          placeholder="设备类型（默认 标准型A）"
          value={furnaceType || undefined}
          onChange={(v) => setFurnaceType(v ?? "")}
          allowClear
          style={{ width: 200 }}
          options={ftypes.map((t) => ({ value: t.name, label: t.name }))}
        />
        <Button type="primary" onClick={add}>
          新增
        </Button>
      </Space>
      <Table
        rowKey="code"
        dataSource={items}
        loading={loading}
        pagination={false}
        columns={[
          { title: "ID", dataIndex: "code", width: 100 },
          { title: "地区", dataIndex: "region", width: 140, render: (v: string, r: DictItem) => `${v}${r.regionCode ? `（${r.regionCode}）` : ""}` },
          { title: "工作点编号", dataIndex: "name", width: 100 },
          {
            title: "设备类型",
            dataIndex: "furnaceType",
            width: 200,
            render: (v: string, r: DictItem) => (
              <Select
                key={`${r.code}:${v}`}
                defaultValue={v}
                onChange={(nv) => nv !== v && retype(r, nv)}
                style={{ width: 170 }}
                options={ftypes.map((t) => ({ value: t.name, label: t.name }))}
              />
            ),
          },
          {
            title: "状态",
            dataIndex: "status",
            width: 90,
            render: (v: string) => (v === "active" ? <Tag color="green">启用</Tag> : <Tag color="red">停用</Tag>),
          },
          {
            title: "操作",
            width: 100,
            render: (_: unknown, r: DictItem) =>
              r.status === "active" ? (
                <Popconfirm title={`停用「${r.region}${r.name}」？`} onConfirm={() => setStatus(r, "inactive")}>
                  <Button type="link" danger>
                    停用
                  </Button>
                </Popconfirm>
              ) : (
                <Button type="link" onClick={() => setStatus(r, "active")}>
                  启用
                </Button>
              ),
          },
        ]}
      />
    </div>
  );
}

export default function DictionariesPage({ active = true }: { active?: boolean }) {
  const { invalidate } = usePageCache();
  const previousActive = useRef(active);
  // 重激活抬升 tick：常驻挂载的缓存页切回时 invalidate 会话缓存并重取各 tab，
  // 与其他缓存页的 becameActive 行为保持一致。
  const [refreshTick, setRefreshTick] = useState(0);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) {
      invalidate("/dictionaries");
      setRefreshTick((tick) => tick + 1);
    }
  }, [active, invalidate]);

  return (
    <div>
      <h2 data-testid="dictionaries-title">基础字典维护</h2>
      <p style={{ color: "#888" }}>
        角色 / 职位 / 地区 / 班组 / 设备类型 / 工作点 等基础数据，带唯一带前缀 ID（POS/REG/SQ/FT/FUR），供其他模块以 code 引用；软删除。
        不同地区的同名工作点编号（如 A 地区 1 号工作点与 B 地区 1 号工作点）使用不同的 FUR 编码。
      </p>
      <Tabs
        items={[
          ...KINDS.map((k) => ({ key: k.key, label: k.label, children: <DictTab kind={k.key} codeEditable={k.codeEditable} refreshTick={refreshTick} /> })),
          { key: "furnace", label: "工作点", children: <FurnaceTab refreshTick={refreshTick} /> },
        ]}
      />
    </div>
  );
}
