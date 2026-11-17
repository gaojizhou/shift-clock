import { useState } from "react";
import { Button, Checkbox, Divider, Input, InputNumber, Select, Space } from "antd";
import { ROLE_OPTIONS } from "@fdc/api-client";

export type QuotaRow = {
  post: string;
  squadless: boolean; // 不分班（值班长/维修工）
  linked: boolean; // 分班时 1/2/3 班联动（以 1 班为准）
  caps: [number, number, number]; // 1/2/3 班；不分班用 caps[0]
  visibleRoles: string[]; // 可见角色：该组角色扫码才能看到并选择此岗
};

export function squadlessRow(): QuotaRow {
  return { post: "", squadless: true, linked: true, caps: [1, 1, 1], visibleRoles: ["chief"] };
}
export function squadRow(): QuotaRow {
  return { post: "", squadless: false, linked: true, caps: [1, 1, 1], visibleRoles: ["worker"] };
}

const CAP_W = 116;

/**
 * 编制编辑组件（「编辑当前工作点编制」与「设备类型编制模板」共用）。
 * - 不分班岗位排在最上面，与分班岗位之间有分隔。
 * - 分班岗位一行四列：岗位、1/2/3 班人数；默认联动（仅 1 班可编辑，2/3 班跟随），可取消联动。
 * - reference：传入则在左侧以低可视度展示模板数据（工作点被改动过时对照用）。
 */
export default function QuotaItemsEditor({
  value,
  onChange,
  idPrefix = "",
  posts = [],
  onAddPost,
}: {
  value: QuotaRow[];
  onChange: (next: QuotaRow[]) => void;
  idPrefix?: string;
  posts?: string[];
  onAddPost?: (name: string) => Promise<void> | void;
}) {
  const [quickAdd, setQuickAdd] = useState("");
  const cols = `180px ${CAP_W}px 64px ${CAP_W}px ${CAP_W}px 240px 56px`;

  const patch = (idx: number, p: Partial<QuotaRow>) =>
    onChange(value.map((r, i) => (i === idx ? { ...r, ...p } : r)));

  const setCap = (idx: number, s: 0 | 1 | 2, v: number) => {
    const row = value[idx];
    const caps: [number, number, number] = [...row.caps];
    caps[s] = v;
    if (s === 0 && row.linked) {
      caps[1] = v;
      caps[2] = v;
    }
    patch(idx, { caps });
  };

  const toggleLinked = (idx: number, linked: boolean) => {
    const row = value[idx];
    const caps: [number, number, number] = linked
      ? [row.caps[0], row.caps[0], row.caps[0]]
      : [...row.caps];
    patch(idx, { linked, caps });
  };

  const addThenSelect = async (idx: number) => {
    const name = quickAdd.trim();
    if (!name || !onAddPost) return;
    await onAddPost(name);
    patch(idx, { post: name });
    setQuickAdd("");
  };

  const postSelect = (t: QuotaRow, idx: number) => (
    <Select
      showSearch
      placeholder="选择岗位"
      style={{ width: 150 }}
      value={t.post || undefined}
      data-testid={`${idPrefix}post-${idx}`}
      options={[...new Set([...posts, ...(t.post ? [t.post] : [])])].map((p) => ({
        label: p,
        value: p,
      }))}
      onChange={(v) => patch(idx, { post: v })}
      notFoundContent="无匹配岗位，可在下方新增"
      dropdownRender={(menu) => (
        <>
          {menu}
          <Divider style={{ margin: "4px 0" }} />
          <div style={{ display: "flex", gap: 8, padding: "0 8px 8px" }}>
            <Input
              size="small"
              placeholder="没有想要的岗位？新增"
              value={quickAdd}
              onChange={(e) => setQuickAdd(e.target.value)}
              onKeyDown={(e) => e.stopPropagation()}
              onPressEnter={() => void addThenSelect(idx)}
            />
            <Button
              size="small"
              type="link"
              disabled={!quickAdd.trim() || !onAddPost}
              onClick={() => void addThenSelect(idx)}
            >
              添加
            </Button>
          </div>
        </>
      )}
    />
  );

  const renderRow = (t: QuotaRow, idx: number) => (
    <div
      key={idx}
      style={{ display: "grid", gridTemplateColumns: cols, gap: 8, alignItems: "center", marginBottom: 8 }}
    >
      {postSelect(t, idx)}
      {t.squadless ? (
        <>
          <InputNumber
            min={0}
            max={99}
            addonBefore="人数"
            value={t.caps[0]}
            data-testid={`${idPrefix}cap-${idx}`}
            onChange={(v) => setCap(idx, 0, typeof v === "number" ? v : 0)}
          />
          <div />
          <div />
          <div />
        </>
      ) : (
        <>
          <InputNumber
            min={0}
            max={99}
            addonBefore="1班"
            value={t.caps[0]}
            data-testid={`${idPrefix}cap1-${idx}`}
            onChange={(v) => setCap(idx, 0, typeof v === "number" ? v : 0)}
          />
          <Checkbox
            checked={t.linked}
            data-testid={`${idPrefix}linked-${idx}`}
            onChange={(e) => toggleLinked(idx, e.target.checked)}
          >
            联动
          </Checkbox>
          <InputNumber
            min={0}
            max={99}
            addonBefore="2班"
            disabled={t.linked}
            value={t.caps[1]}
            data-testid={`${idPrefix}cap2-${idx}`}
            onChange={(v) => setCap(idx, 1, typeof v === "number" ? v : 0)}
          />
          <InputNumber
            min={0}
            max={99}
            addonBefore="3班"
            disabled={t.linked}
            value={t.caps[2]}
            data-testid={`${idPrefix}cap3-${idx}`}
            onChange={(v) => setCap(idx, 2, typeof v === "number" ? v : 0)}
          />
        </>
      )}
      <Select
        mode="multiple"
        allowClear
        placeholder="可见角色（扫码可选）"
        style={{ width: 240 }}
        maxTagCount="responsive"
        value={t.visibleRoles}
        data-testid={`${idPrefix}visible-roles-${idx}`}
        options={ROLE_OPTIONS}
        onChange={(v) => patch(idx, { visibleRoles: v as string[] })}
      />
      <Button danger size="small" onClick={() => onChange(value.filter((_, i) => i !== idx))}>
        删除
      </Button>
    </div>
  );

  const indexed = value.map((r, i) => ({ r, i }));
  const squadless = indexed.filter((x) => x.r.squadless);
  const squad = indexed.filter((x) => !x.r.squadless);

  return (
    <div>
      <div style={{ fontSize: 12, color: "#888", marginBottom: 6 }}>不分班岗位（值班长/维修工等）</div>
      {squadless.map((x) => renderRow(x.r, x.i))}
      {squadless.length === 0 && <div style={{ color: "#ccc", fontSize: 12, marginBottom: 8 }}>（无）</div>}

      <Divider style={{ margin: "8px 0" }} />
      <div style={{ fontSize: 12, color: "#888", marginBottom: 6 }}>分班岗位（1/2/3 班）</div>
      {squad.map((x) => renderRow(x.r, x.i))}
      {squad.length === 0 && <div style={{ color: "#ccc", fontSize: 12, marginBottom: 8 }}>（无）</div>}

      <Space style={{ marginTop: 8 }}>
        <Button data-testid={`${idPrefix}add-squadless`} onClick={() => onChange([...value, squadlessRow()])}>
          新增不分班岗位
        </Button>
        <Button type="dashed" data-testid={`${idPrefix}add-squad`} onClick={() => onChange([...value, squadRow()])}>
          新增分班岗位
        </Button>
      </Space>
    </div>
  );
}
