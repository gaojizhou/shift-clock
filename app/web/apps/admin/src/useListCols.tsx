import { useState } from "react";
import { Checkbox, Space } from "antd";

export type ColPrefs = { id: boolean; updatedAt: boolean; updatedBy: boolean };

/** 列表页「是否展示 ID/修改时间/修改人」偏好，记忆在 localStorage（默认不展示）。 */
export function useListCols(key: string, def: Partial<ColPrefs> = {}): {
  prefs: ColPrefs;
  Toggles: () => JSX.Element;
} {
  const storeKey = `cols:${key}`;
  const initial: ColPrefs = { id: false, updatedAt: false, updatedBy: false, ...def };
  const [prefs, setPrefs] = useState<ColPrefs>(() => {
    try {
      const s = localStorage.getItem(storeKey);
      return s ? { ...initial, ...(JSON.parse(s) as Partial<ColPrefs>) } : initial;
    } catch {
      return initial;
    }
  });
  const set = (p: Partial<ColPrefs>) => {
    const next = { ...prefs, ...p };
    setPrefs(next);
    try {
      localStorage.setItem(storeKey, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  };
  const Toggles = () => (
    <Space>
      <span style={{ color: "#888" }}>列显示：</span>
      <Checkbox checked={prefs.id} onChange={(e) => set({ id: e.target.checked })}>
        ID
      </Checkbox>
      <Checkbox checked={prefs.updatedAt} onChange={(e) => set({ updatedAt: e.target.checked })}>
        修改时间
      </Checkbox>
      <Checkbox checked={prefs.updatedBy} onChange={(e) => set({ updatedBy: e.target.checked })}>
        修改人
      </Checkbox>
    </Space>
  );
  return { prefs, Toggles };
}

export function fmtTime(s?: string | null): string {
  if (!s) return "-";
  return s.replace("T", " ").slice(0, 19);
}
