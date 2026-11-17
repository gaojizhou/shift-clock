import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Card, DatePicker, Input, Modal, Select, Space, Spin, Table, Tag, Tooltip, Typography, message } from "antd";
import type { Dayjs } from "dayjs";
import { getAuditLogs, type AuditLogItem } from "@fdc/api-client";
import { useTableColumns } from "../useTableColumns";
import { usePageCache } from "../pageCache";

const { RangePicker } = DatePicker;

const ACTION_COLORS: Record<string, string> = {
  makeup_proxy_signin: "volcano",
  makeup_review: "geekblue",
  confirm_exclude: "red",
  confirm_submit: "green",
  anomaly_resolve: "purple",
  scan: "default",
  request_ok: "green",
  request_error: "red",
  request_unauthenticated: "gold",
  request_forbidden: "orange",
  request_rejected: "volcano",
};

const ROLE_COLORS: Record<string, string> = {
  chief: "blue",
  leader: "green",
  stat: "cyan",
  hr: "gold",
  admin: "magenta",
  worker: "default",
};

function fmtTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
    d.getMinutes()
  )}:${p(d.getSeconds())}`;
}

function requestDetailText(item: AuditLogItem): string {
  return JSON.stringify(item.requestDetail ?? {}, null, 2);
}

export default function AuditLogsPage({ active = true }: { active?: boolean }) {
  const { invalidate } = usePageCache();
  const previousActive = useRef(active);
  const [items, setItems] = useState<AuditLogItem[]>([]);
  const [actions, setActions] = useState<{ value: string; label: string }[]>([]);
  const [filter, setFilter] = useState<string>("");
  const [actor, setActor] = useState("");
  const [target, setTarget] = useState("");
  const [detail, setDetail] = useState("");
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailItem, setDetailItem] = useState<AuditLogItem | null>(null);
  useEffect(() => {
    if (!active) setDetailItem(null);
  }, [active]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getAuditLogs({
        action: filter || undefined,
        actor: actor.trim() || undefined,
        target: target.trim() || undefined,
        detail: detail.trim() || undefined,
        start: range?.[0]?.format("YYYY-MM-DD"),
        end: range?.[1]?.format("YYYY-MM-DD"),
        limit: 300,
      });
      setItems(res.items);
      if (res.actions.length) setActions(res.actions);
    } catch (e) {
      message.error((e as Error).message || "加载操作日志失败");
    } finally {
      setLoading(false);
    }
  }, [filter, actor, target, detail, range]);

  // 文本输入防抖：筛选项变化后 250ms 再请求，避免逐字符打接口
  useEffect(() => {
    const t = setTimeout(() => void load(), 250);
    return () => clearTimeout(t);
  }, [load]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) {
      invalidate("/audit-logs");
      void load();
    }
  }, [active, invalidate, load]);

  const columns = [
    {
      title: "时间",
      dataIndex: "ts",
      key: "ts",
      width: 180,
      render: (ts: string | null) => fmtTime(ts),
    },
    {
      title: "操作人",
      key: "actor",
      width: 180,
      render: (_: unknown, r: AuditLogItem) => (
        <Space size={4}>
          <Tag color={ROLE_COLORS[r.actorRole] || "default"}>{r.actorRoleLabel}</Tag>
          <span>{r.actorName || "-"}</span>
          <span style={{ color: "#999" }}>{r.actor}</span>
        </Space>
      ),
    },
    {
      title: "操作类型",
      dataIndex: "actionLabel",
      key: "action",
      width: 130,
      render: (label: string, r: AuditLogItem) => (
        <Space size={4}>
          <Tag color={ACTION_COLORS[r.action] || "default"}>{label}</Tag>
          {r.requestStatus != null && <Tag>{r.requestStatus}</Tag>}
        </Space>
      ),
    },
    {
      title: "对象",
      key: "target",
      width: 160,
      render: (_: unknown, r: AuditLogItem) => {
        if (r.empId) return `${r.empName || ""} ${r.empId}`.trim();
        if (r.furnace) return r.furnace;
        return "-";
      },
    },
    {
      title: "详情",
      dataIndex: "detail",
      key: "detail",
      width: 280,
      render: (detail: string, r: AuditLogItem) => (
        <Space size={8}>
          <Tooltip title={detail}>
            <span>{detail || "-"}</span>
          </Tooltip>
          {r.requestDetail && (
            <Button size="small" onClick={() => setDetailItem(r)}>
              请求详情
            </Button>
          )}
        </Space>
      ),
    },
  ];

  // 列显示开关 + 列宽拖拽（持久化）
  const tc = useTableColumns("audit-logs", [
    { key: "ts", title: "时间" },
    { key: "actor", title: "操作人" },
    { key: "action", title: "操作类型" },
    { key: "target", title: "对象" },
    { key: "detail", title: "详情" },
  ]);
  const dec = tc.decorate<AuditLogItem>(columns as never);

  const segOptions = [{ label: "全部", value: "" }, ...actions.map((a) => ({ label: a.label, value: a.value }))];

  return (
    <Card
      title="操作日志"
      extra={
        <span style={{ color: "#999", fontSize: 12 }}>
          记录代签、补录审批、取消打卡、日结确认、异常处置等人员操作（人事 / 统计可见）
        </span>
      }
    >
      <Space direction="vertical" style={{ width: "100%" }} size="middle">
        <Space wrap size={[8, 8]}>
          <Space>
            <span>操作类型</span>
            <Select
              style={{ minWidth: 160 }}
              options={segOptions}
              value={filter}
              onChange={(v) => setFilter(String(v))}
            />
          </Space>
          <Space>
            <span>时间</span>
            <RangePicker
              value={range}
              onChange={(v) => setRange(v as [Dayjs | null, Dayjs | null] | null)}
              allowClear
            />
          </Space>
          <Input
            style={{ width: 170 }}
            placeholder="操作人（工号/姓名）"
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            allowClear
          />
          <Input
            style={{ width: 190 }}
            placeholder="对象（工号/姓名/工作点编号）"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            allowClear
          />
          <Input
            style={{ width: 200 }}
            placeholder="详情关键字"
            value={detail}
            onChange={(e) => setDetail(e.target.value)}
            allowClear
          />
          <tc.Toggles />
        </Space>
        {loading ? (
          <div style={{ textAlign: "center", padding: 48 }}>
            <Spin />
          </div>
        ) : (
          <Table
            rowKey="id"
            size="small"
            columns={dec.columns}
            components={tc.components}
            scroll={{ x: dec.scrollX }}
            dataSource={items}
            pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
          />
        )}
      </Space>
      <Modal
        title="请求详情"
        open={!!detailItem}
        onCancel={() => setDetailItem(null)}
        footer={null}
        width={760}
      >
        {detailItem && (
          <Space direction="vertical" style={{ width: "100%" }} size="small">
            <Space wrap>
              <Tag color={ACTION_COLORS[detailItem.action] || "default"}>{detailItem.actionLabel}</Tag>
              {detailItem.requestStatus != null && <Tag>{detailItem.requestStatus}</Tag>}
              <span>{fmtTime(detailItem.ts)}</span>
            </Space>
            <Typography.Paragraph copyable style={{ whiteSpace: "pre-wrap", maxHeight: 520, overflow: "auto" }}>
              {requestDetailText(detailItem)}
            </Typography.Paragraph>
          </Space>
        )}
      </Modal>
    </Card>
  );
}
