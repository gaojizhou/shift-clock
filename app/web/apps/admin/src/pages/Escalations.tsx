import { useCallback, useEffect, useRef, useState } from "react";
import { Button, DatePicker, Space, Table, Tag, message } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { getEscalations, runEscalation, type EscalationItem } from "@fdc/api-client";
import { usePageCache } from "../pageCache";

const STATUS_COLORS: Record<string, string> = {
  open: "red",
  resolved: "green",
  closed: "default",
};

export default function EscalationsPage({ active = true }: { active?: boolean }) {
  const { invalidate } = usePageCache();
  const previousActive = useRef(active);
  const [date, setDate] = useState<Dayjs>(dayjs());
  const [items, setItems] = useState<EscalationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);

  const dateStr = date.format("YYYY-MM-DD");

  const load = useCallback(() => {
    setLoading(true);
    getEscalations(dateStr)
      .then((r) => setItems(r.items))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, [dateStr]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) {
      invalidate("/escalations");
      load();
    }
  }, [active, invalidate, load]);

  const onRun = async () => {
    setRunning(true);
    try {
      await runEscalation(dateStr);
      message.success("升级检测已执行");
      load();
    } catch (e) {
      message.error((e as { data?: { detail?: string } }).data?.detail || "执行失败");
    } finally {
      setRunning(false);
    }
  };

  const cols = [
    { title: "ID", dataIndex: "id", width: 70 },
    { title: "工作点ID", dataIndex: "furnaceId", width: 90 },
    { title: "日期", dataIndex: "date", width: 120 },
    { title: "级别", dataIndex: "level", width: 80, render: (v: number) => `L${v}` },
    { title: "上报对象", dataIndex: "targets" },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (s: string) => <Tag color={STATUS_COLORS[s] || "default"}>{s}</Tag>,
    },
  ];

  return (
    <div>
      <h2 data-testid="escalations-title">升级记录</h2>
      <Space style={{ marginBottom: 16 }}>
        <DatePicker
          value={date}
          onChange={(d) => d && setDate(d)}
          allowClear={false}
          data-testid="escalations-date"
        />
        <Button type="primary" loading={running} onClick={onRun} data-testid="escalations-run">
          执行升级检测
        </Button>
      </Space>
      <Table
        rowKey="id"
        loading={loading}
        columns={cols}
        dataSource={items}
        pagination={false}
        size="small"
        locale={{ emptyText: "当日无升级记录" }}
      />
    </div>
  );
}
