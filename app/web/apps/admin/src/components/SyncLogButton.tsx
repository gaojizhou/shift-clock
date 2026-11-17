import { useEffect, useRef, useState } from "react";
import { Alert, Button, DatePicker, Descriptions, Drawer, Select, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";
import { getSyncExecutionLogs, type SyncExecutionLog, type SyncExecutionLogs } from "@fdc/api-client";

const states = { running: "执行中", success: "成功", partial: "部分完成", failed: "失败", skipped: "已跳过" };
const colors = { running: "processing", success: "success", partial: "warning", failed: "error", skipped: "default" };
const time = (value: string | null) => value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "—";

export default function SyncLogButton({ group, active = true }: { group: "attendance" | "employee"; active?: boolean }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<SyncExecutionLogs>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [kind, setKind] = useState<string>();
  const [trigger, setTrigger] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [refresh, setRefresh] = useState(0);
  const sequence = useRef(0);

  useEffect(() => {
    if (!open || !active) return;
    let live = true;
    const load = async (quiet = false) => {
      const current = ++sequence.current;
      if (!quiet) setLoading(true);
      try {
        const result = await getSyncExecutionLogs({ group, page, pageSize: 30, kind, trigger, status,
          dateFrom: range?.[0].format("YYYY-MM-DD"), dateTo: range?.[1].format("YYYY-MM-DD") });
        if (live && sequence.current === current) { setData(result); setError(""); }
      } catch (e) {
        if (live && sequence.current === current) setError((e as Error).message || "加载同步日志失败");
      } finally {
        if (live && sequence.current === current) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(true), 10000);
    return () => { live = false; window.clearInterval(timer); };
  }, [open, active, group, page, kind, trigger, status, range, refresh]);

  const columns: ColumnsType<SyncExecutionLog> = [
    { title: "开始时间", dataIndex: "startedAt", width: 170, render: time },
    { title: "结束时间", dataIndex: "finishedAt", width: 170, render: time },
    { title: "同步任务", dataIndex: "kindLabel", width: 180 },
    { title: "触发方式", dataIndex: "trigger", width: 90, render: v => v === "auto" ? <Tag color="blue">自动</Tag> : <Tag>手动</Tag> },
    { title: "结果", dataIndex: "status", width: 100, render: (v: SyncExecutionLog["status"]) => <Tag color={colors[v]}>{states[v]}</Tag> },
    { title: "拉取条数", dataIndex: "fetched", width: 100, render: (v, row) => row.status === "running" ? "统计中" : v },
    { title: "入库条数", dataIndex: "upserted", width: 100, render: (v, row) => row.status === "running" ? "统计中" : v },
    { title: "操作人", dataIndex: "actor", width: 110, render: v => v === "scheduler" ? "系统定时任务" : v },
    { title: "数据日期范围", width: 190, render: (_, row) => row.dateFrom ? `${row.dateFrom} ~ ${row.dateTo}` : "—" },
    { title: "说明", dataIndex: "message", width: 220, ellipsis: true },
  ];

  return <>
    <Button onClick={() => { setOpen(true); setRefresh(v => v + 1); }}>同步日志</Button>
    <Drawer title={group === "attendance" ? "考勤同步日志" : "员工同步日志"} open={open && active} onClose={() => setOpen(false)} width="min(1500px, 96vw)">
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Alert type="info" showIcon message="记录手动拉取和自动同步；展开一条记录可查看各类数据数量。"
          description="入库包括新增和更新，不等于新增条数。拉取总量包含各来源返回的数据，重复拉取会再次计数。日志从本功能上线起记录；执行中的数量在任务结束后汇总。" />
        <Space wrap>
          <Select aria-label="同步类型" placeholder="全部同步类型" allowClear value={kind} style={{ width: 210 }}
            options={Object.entries(data?.kinds || {}).map(([value, label]) => ({ value, label }))}
            onChange={v => { setKind(v); setPage(1); }} />
          <Select aria-label="触发方式" placeholder="全部触发方式" allowClear value={trigger} style={{ width: 150 }}
            options={[{ value: "auto", label: "自动" }, { value: "manual", label: "手动" }]}
            onChange={v => { setTrigger(v); setPage(1); }} />
          <Select aria-label="执行结果" placeholder="全部执行结果" allowClear value={status} style={{ width: 150 }}
            options={Object.entries(states).map(([value, label]) => ({ value, label }))}
            onChange={v => { setStatus(v); setPage(1); }} />
          <DatePicker.RangePicker value={range} placeholder={["执行开始日期", "执行结束日期"]}
            onChange={v => { setRange(v?.[0] && v[1] ? [v[0], v[1]] : null); setPage(1); }} />
          <Button onClick={() => setRefresh(v => v + 1)} loading={loading}>刷新</Button>
          <Button onClick={() => { setKind(undefined); setTrigger(undefined); setStatus(undefined); setRange(null); setPage(1); }}>重置</Button>
        </Space>
        {error && <Alert type="error" showIcon message={error} />}
        <Table rowKey="id" columns={columns} dataSource={data?.items || []} loading={loading} size="small" scroll={{ x: 1530 }}
          locale={{ emptyText: "暂无同步记录" }}
          pagination={{ current: page, total: data?.total || 0, pageSize: 30, showSizeChanger: false, showTotal: total => `共 ${total} 条`, onChange: setPage }}
          expandable={{ expandedRowRender: row => <Descriptions bordered size="small" column={2}>
            <Descriptions.Item label="执行编号">{row.id}</Descriptions.Item>
            <Descriptions.Item label="地区">{row.region || "全部地区"}</Descriptions.Item>
            <Descriptions.Item label="耗时">{row.finishedAt ? `${Math.max(0, dayjs(row.finishedAt).diff(dayjs(row.startedAt), "second"))} 秒` : "尚未结束"}</Descriptions.Item>
            {Object.entries(row.counts).map(([key, value]) => <Descriptions.Item key={key} label={data?.countLabels[key] || key}>{value}</Descriptions.Item>)}
            <Descriptions.Item label="说明 / 失败原因" span={2}>{row.message || "—"}</Descriptions.Item>
          </Descriptions> }} />
      </Space>
    </Drawer>
  </>;
}
