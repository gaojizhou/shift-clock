import { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  Card,
  Input,
  InputNumber,
  Modal,
  Space,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import {
  getDemoContext,
  getProxyConfig,
  getProxyUsage,
  grantProxyExtra,
  setProxyConfig,
  type ProxyUsageRow,
} from "@fdc/api-client";
import { usePageCache } from "../pageCache";

function thisMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export default function ProxyQuotaPage({ active = true }: { active?: boolean }) {
  const { invalidate } = usePageCache();
  const previousActive = useRef(active);
  const [month, setMonth] = useState<string>(thisMonth());
  const [defaultLimit, setDefaultLimit] = useState<number>(3);
  const [editLimit, setEditLimit] = useState<number>(3);
  const [rows, setRows] = useState<ProxyUsageRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [savingLimit, setSavingLimit] = useState(false);

  // grant modal
  const [grantOpen, setGrantOpen] = useState(false);
  const [grantEmp, setGrantEmp] = useState<string>("");
  const [grantName, setGrantName] = useState<string>("");
  const [grantExtra, setGrantExtra] = useState<number>(1);
  const [grantReason, setGrantReason] = useState<string>("");
  useEffect(() => {
    if (!active) setGrantOpen(false);
  }, [active]);

  useEffect(() => {
    getDemoContext()
      .then((c) => c?.month && setMonth(c.month))
      .catch(() => {});
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const c = await getProxyConfig();
      setDefaultLimit(c.defaultLimit);
      setEditLimit(c.defaultLimit);
    } catch (e) {
      message.error((e as Error).message || "读取默认值失败");
    }
  }, []);

  const loadUsage = useCallback(async (m: string) => {
    setLoading(true);
    try {
      const u = await getProxyUsage(m);
      setRows(u.items);
      setDefaultLimit(u.default);
    } catch (e) {
      message.error((e as Error).message || "读取用量失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  useEffect(() => {
    void loadUsage(month);
  }, [month, loadUsage]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) {
      invalidate("/proxy-quota");
      void loadConfig();
      void loadUsage(month);
    }
  }, [active, invalidate, loadConfig, loadUsage, month]);

  const saveLimit = async () => {
    setSavingLimit(true);
    try {
      const r = await setProxyConfig(editLimit);
      setDefaultLimit(r.defaultLimit);
      message.success(`默认上限已更新为 ${r.defaultLimit}`);
      void loadUsage(month);
    } catch (e) {
      message.error((e as Error).message || "保存失败");
    } finally {
      setSavingLimit(false);
    }
  };

  const openGrant = (emp: string, name: string | null) => {
    setGrantEmp(emp);
    setGrantName(name || "");
    setGrantExtra(1);
    setGrantReason("");
    setGrantOpen(true);
  };

  const submitGrant = async () => {
    if (!grantEmp.trim()) {
      message.warning("请填写工号");
      return;
    }
    try {
      await grantProxyExtra(grantEmp.trim(), month, grantExtra, grantReason.trim() || undefined);
      message.success(`已为 ${grantEmp} 在 ${month} 加量 +${grantExtra}`);
      setGrantOpen(false);
      void loadUsage(month);
    } catch (e) {
      message.error((e as Error).message || "加量失败");
    }
  };

  const columns = [
    { title: "工号", dataIndex: "empId", key: "empId", width: 120 },
    { title: "姓名", dataIndex: "name", key: "name", width: 120, render: (n: string | null) => n || "-" },
    { title: "本月已代签", dataIndex: "used", key: "used", width: 110 },
    { title: "默认上限", dataIndex: "default", key: "default", width: 100 },
    {
      title: "临时加量",
      dataIndex: "extra",
      key: "extra",
      width: 100,
      render: (x: number) => (x > 0 ? <Tag color="gold">+{x}</Tag> : "-"),
    },
    { title: "有效上限", dataIndex: "limit", key: "limit", width: 100 },
    {
      title: "余量",
      dataIndex: "remaining",
      key: "remaining",
      width: 100,
      render: (r: number, row: ProxyUsageRow) => (
        <Tag color={row.atLimit ? "red" : r <= 1 ? "orange" : "green"}>{r}</Tag>
      ),
    },
    {
      title: "操作",
      key: "op",
      render: (_: unknown, row: ProxyUsageRow) => (
        <Button size="small" type="link" onClick={() => openGrant(row.empId, row.name)}>
          加量
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="large">
      <Card title="代签默认上限（全局）">
        <Space>
          <span>每人每月代签上限：</span>
          <InputNumber min={0} value={editLimit} onChange={(v) => setEditLimit(Number(v ?? 0))} />
          <Button type="primary" loading={savingLimit} onClick={saveLimit}>
            保存
          </Button>
          <span style={{ color: "#999" }}>当前生效：{defaultLimit}</span>
        </Space>
        <div style={{ color: "#999", fontSize: 12, marginTop: 8 }}>
          修改后对所有人当月立即生效；改动记入操作日志。临时给个人加量请在下方表格操作。
        </div>
      </Card>

      <Card
        title="代签用量"
        extra={
          <Space>
            <span>月份</span>
            <Input
              style={{ width: 120 }}
              value={month}
              placeholder="YYYY-MM"
              onChange={(e) => setMonth(e.target.value)}
            />
            <Button onClick={() => openGrant("", null)}>手动加量</Button>
          </Space>
        }
      >
        <Table
          rowKey="empId"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 人` }}
          locale={{ emptyText: "本月暂无代签记录" }}
        />
        <div style={{ color: "#999", fontSize: 12, marginTop: 8 }}>
          <Tooltip title="按考勤日所属月统计">用量 = 本月成功代签次数</Tooltip>
          ；有效上限 = 默认上限 + 临时加量。
        </div>
      </Card>

      <Modal
        title={`临时加量${grantName ? ` · ${grantName}` : ""}`}
        open={grantOpen}
        onOk={submitGrant}
        onCancel={() => setGrantOpen(false)}
        okText="确认加量"
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <div>
            <div style={{ marginBottom: 4 }}>工号</div>
            <Input
              value={grantEmp}
              disabled={!!grantName}
              placeholder="如 T0001"
              onChange={(e) => setGrantEmp(e.target.value)}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>月份</div>
            <Input value={month} disabled />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>增加次数</div>
            <InputNumber min={1} value={grantExtra} onChange={(v) => setGrantExtra(Number(v ?? 1))} />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>原因</div>
            <Input.TextArea
              rows={2}
              value={grantReason}
              placeholder="如：手机损坏待修"
              onChange={(e) => setGrantReason(e.target.value)}
            />
          </div>
        </Space>
      </Modal>
    </Space>
  );
}
