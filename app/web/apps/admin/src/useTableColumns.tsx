import { useRef, useState, type ReactNode } from "react";
import { Button, Checkbox, Popover, Space } from "antd";
import type { ColumnsType } from "antd/es/table";

/**
 * 多列表格的「列显示开关 + 列宽拖拽」偏好，记忆在 localStorage（默认沿用上次）。
 * - 列开关：勾选/取消即时生效并持久化；
 * - 列宽：拖表头右缘调整，持久化；可一键重置。
 * 自包含实现（不引入 react-resizable）。每列需有稳定 key。
 */
type Prefs = { hidden: string[]; widths: Record<string, number> };
type ColMeta = { key: string; title: ReactNode; defaultHidden?: boolean };

// 自定义可拖拽表头单元（th）：右缘 8px 把手拖动改宽
function ResizableTitle(props: Record<string, unknown>) {
  const { onResize, width, ...rest } = props as {
    onResize?: (w: number) => void;
    width?: number;
    style?: React.CSSProperties;
    children?: ReactNode;
  };
  if (width == null || !onResize) return <th {...(rest as object)} />;
  const onMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = width;
    const move = (ev: MouseEvent) => onResize(startW + ev.clientX - startX);
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
    document.body.style.cursor = "col-resize";
  };
  return (
    <th {...(rest as object)} style={{ ...(rest.style || {}), position: "relative" }}>
      {rest.children}
      <span
        onMouseDown={onMouseDown}
        style={{ position: "absolute", top: 0, right: 0, bottom: 0, width: 8, cursor: "col-resize", userSelect: "none" }}
      />
    </th>
  );
}

export function useTableColumns(storeKey: string, metas: ColMeta[]) {
  const SK = `tablecols:${storeKey}`;
  // 标记了 defaultHidden 的列首次（无本地偏好时）默认隐藏，用户可在列开关里勾选开启并持久化
  const defaultHidden = metas.filter((m) => m.defaultHidden).map((m) => m.key);
  const [prefs, setPrefs] = useState<Prefs>(() => {
    try {
      const s = localStorage.getItem(SK);
      return s
        ? { hidden: [], widths: {}, ...(JSON.parse(s) as Partial<Prefs>) }
        : { hidden: defaultHidden, widths: {} };
    } catch {
      return { hidden: defaultHidden, widths: {} };
    }
  });
  const ref = useRef(prefs);
  ref.current = prefs;
  const update = (fn: (p: Prefs) => Prefs) =>
    setPrefs((prev) => {
      const next = fn(prev);
      try {
        localStorage.setItem(SK, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  const toggle = (k: string, on: boolean) =>
    update((p) => ({ ...p, hidden: on ? p.hidden.filter((x) => x !== k) : [...new Set([...p.hidden, k])] }));
  const setWidth = (k: string, w: number) =>
    update((p) => ({ ...p, widths: { ...p.widths, [k]: Math.max(56, Math.round(w)) } }));
  const reset = () => update(() => ({ hidden: defaultHidden, widths: {} }));

  const components = { header: { cell: ResizableTitle } };

  // 把原始列定义按偏好处理：隐藏过滤、注入宽度、挂可拖拽表头
  function decorate<T>(columns: ColumnsType<T>): { columns: ColumnsType<T>; scrollX: number } {
    let total = 0;
    const out = (columns as Array<Record<string, unknown>>)
      .filter((c) => {
        const key = String(c.key ?? c.dataIndex ?? "");
        return !prefs.hidden.includes(key);
      })
      .map((c) => {
        const key = String(c.key ?? c.dataIndex ?? "");
        const w = prefs.widths[key] ?? (c.width as number) ?? 120;
        total += w;
        return {
          ...c,
          key,
          width: w,
          onHeaderCell: () => ({ width: w, onResize: (nw: number) => setWidth(key, nw) }),
        };
      });
    return { columns: out as unknown as ColumnsType<T>, scrollX: total };
  }

  const Toggles = () => (
    <Popover
      trigger="click"
      placement="bottomRight"
      content={
        <div style={{ maxWidth: 260 }}>
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            {metas.map((m) => (
              <Checkbox
                key={m.key}
                checked={!prefs.hidden.includes(m.key)}
                onChange={(e) => toggle(m.key, e.target.checked)}
              >
                {m.title}
              </Checkbox>
            ))}
            <Button size="small" onClick={reset} style={{ marginTop: 6 }}>
              重置列显示与宽度
            </Button>
          </Space>
        </div>
      }
    >
      <Button size="small">列设置 ⚙</Button>
    </Popover>
  );

  return { decorate, components, Toggles };
}
