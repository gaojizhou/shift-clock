import { useCallback, useEffect, useState } from "react";
import { Button, Card, DatePicker, Input, Select, Space, Table, Tag, message } from "antd";
import dayjs from "dayjs";
import {
  getFurnacePushStatus,
  getPushLogs,
  sendTestPush,
  type FurnacePushStatus,
  type PushLog,
} from "@fdc/api-client";

// 一行 = 一个 (工作点, 值班长) 对；无值班长的工作点单独占一行（hasChief=false）
type Row = {
  key: string;
  furnaceLabel: string;
  hasChief: boolean;
  empId?: string;
  chiefName?: string;
  ddUserId?: string;
  hasDing?: boolean;
};

function fmtTs(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
    d.getMinutes()
  )}:${p(d.getSeconds())}`;
}

function StatusTag({ status, reason }: { status: string; reason?: string }) {
  if (status === "sent") return <Tag color="green">已送达</Tag>;
  if (status === "failed")
    return <Tag color="red">投递失败{reason ? `·${reason}` : ""}</Tag>;
  return <Tag color="orange">未推送{reason ? `·${reason}` : ""}</Tag>;
}

export function PushDebugSection() {
  const [items, setItems] = useState<FurnacePushStatus[]>([]);
  const [logs, setLogs] = useState<PushLog[]>([]);
  const [logKinds, setLogKinds] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [logLoading, setLogLoading] = useState(false);
  const [sendingKey, setSendingKey] = useState("");
  // 推送日志筛选：类型 / 日期 / 统一搜索框(目标名字·钉钉ID·工号·内容)
  const [fKind, setFKind] = useState<string | undefined>(undefined);
  const [fDate, setFDate] = useState<string | undefined>(undefined);
  const [fq, setFq] = useState("");
  const [fqInput, setFqInput] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    getFurnacePushStatus()
      .then((r) => setItems(r.items))
      .catch(() => message.error("加载失败（需超管）"))
      .finally(() => setLoading(false));
  }, []);

  const loadLogs = useCallback(() => {
    setLogLoading(true);
    getPushLogs({ kind: fKind, date: fDate, q: fq || undefined })
      .then((r) => {
        setLogs(r.items);
        if (r.kinds?.length) setLogKinds(r.kinds);
      })
      .catch(() => undefined)
      .finally(() => setLogLoading(false));
  }, [fKind, fDate, fq]);

  // 搜索框防抖：停止输入 400ms 后再查
  useEffect(() => {
    const t = setTimeout(() => setFq(fqInput.trim()), 400);
    return () => clearTimeout(t);
  }, [fqInput]);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    loadLogs();
  }, [loadLogs]);

  const rows: Row[] = [];
  for (const f of items) {
    const label = `${f.region}·${f.furnaceNo}`;
    if (!f.hasChief) {
      rows.push({ key: `f${f.furnaceId}`, furnaceLabel: label, hasChief: false });
    } else {
      f.chiefs.forEach((c, i) => {
        rows.push({
          key: `f${f.furnaceId}-${c.empId}-${i}`,
          furnaceLabel: label,
          hasChief: true,
          empId: c.empId,
          chiefName: c.name,
          ddUserId: c.ddUserId,
          hasDing: c.hasDing,
        });
      });
    }
  }

  const doTest = async (row: Row) => {
    if (!row.empId) return;
    setSendingKey(row.key);
    try {
      const r = await sendTestPush(row.empId);
      if (r.ok) message.success(`已推送给 ${row.chiefName}`);
      else message.error(`推送失败：${r.error || "未知原因"}`);
    } catch {
      message.error("推送失败（需超管）");
    } finally {
      setSendingKey("");
      loadLogs();
    }
  };

  const noChief = items.filter((f) => !f.hasChief).length;
  const noDing = items.reduce((a, f) => a + f.chiefs.filter((c) => !c.hasDing).length, 0);

  return (
    <>
      <Card
        title="工作点推送状态"
        style={{ marginBottom: 16 }}
        extra={
          <Space>
            <span style={{ color: "#999", fontSize: 12 }}>
              共 {items.length} 工作点 · {noChief} 无值班长 · {noDing} 值班长无钉钉号
            </span>
            <Button size="small" onClick={load}>
              刷新
            </Button>
          </Space>
        }
      >
        <Table
          size="small"
          rowKey="key"
          loading={loading}
          dataSource={rows}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 行` }}
          columns={[
            { title: "工作点", dataIndex: "furnaceLabel", width: 200 },
            {
              title: "值班长",
              width: 220,
              render: (_: unknown, r: Row) =>
                r.hasChief ? (
                  `${r.chiefName}（${r.empId}）`
                ) : (
                  <Tag color="orange">未绑定值班长</Tag>
                ),
            },
            {
              title: "钉钉号",
              width: 240,
              render: (_: unknown, r: Row) => {
                if (!r.hasChief) return "—";
                return r.hasDing ? (
                  <Space>
                    <Tag color="green">有钉钉号</Tag>
                    <span style={{ color: "#999" }}>{r.ddUserId}</span>
                  </Space>
                ) : (
                  <Tag color="red">无钉钉号 · 无法推送</Tag>
                );
              },
            },
            {
              title: "测试推送",
              width: 120,
              render: (_: unknown, r: Row) => (
                <Button
                  size="small"
                  type="primary"
                  disabled={!r.hasChief || !r.hasDing}
                  loading={sendingKey === r.key}
                  onClick={() => doTest(r)}
                >
                  测试推送
                </Button>
              ),
            },
          ]}
        />
      </Card>

      <Card
        title="推送日志"
        extra={
          <Space wrap>
            <Select
              allowClear
              size="small"
              style={{ width: 140 }}
              placeholder="类型"
              value={fKind}
              onChange={(v) => setFKind(v)}
              options={logKinds.map((k) => ({ value: k, label: k }))}
            />
            <DatePicker
              size="small"
              placeholder="日期"
              value={fDate ? dayjs(fDate) : null}
              onChange={(d) => setFDate(d ? d.format("YYYY-MM-DD") : undefined)}
            />
            <Input
              allowClear
              size="small"
              style={{ width: 200 }}
              placeholder="搜索：目标名字/钉钉ID/工号/内容"
              value={fqInput}
              onChange={(e) => setFqInput(e.target.value)}
            />
            <Button size="small" onClick={loadLogs}>
              刷新
            </Button>
          </Space>
        }
      >
        <Table
          size="small"
          rowKey="id"
          loading={logLoading}
          dataSource={logs}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
          expandable={{
            expandedRowRender: (r: PushLog) => (
              <div>
                <div style={{ marginBottom: 6, color: "#666", fontSize: 12 }}>
                  钉钉原始投递回执（taskId / forbidden / invalidUsers / raw）：
                </div>
                <pre
                  style={{
                    margin: 0,
                    padding: 8,
                    background: "#f6f8fa",
                    borderRadius: 6,
                    fontSize: 12,
                    maxHeight: 320,
                    overflow: "auto",
                  }}
                >
                  {r.result && Object.keys(r.result).length > 0
                    ? JSON.stringify(r.result, null, 2)
                    : "无回执（未尝试发送，如：值班长无钉钉号）"}
                </pre>
                {r.text ? (
                  <div style={{ marginTop: 8 }}>
                    <span style={{ color: "#666", fontSize: 12 }}>推送内容：</span>
                    <div style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{r.text}</div>
                  </div>
                ) : null}
              </div>
            ),
            rowExpandable: () => true,
          }}
          columns={[
            { title: "时间", width: 160, render: (_: unknown, r: PushLog) => fmtTs(r.ts) },
            {
              title: "类型",
              width: 110,
              render: (_: unknown, r: PushLog) => {
                if (r.kind === "test_push") return <Tag color="purple">测试</Tag>;
                if (r.kind === "shift_push")
                  return <Tag color="blue">定时{r.timePoint ? `·${r.timePoint}` : ""}</Tag>;
                const colorMap: Record<string, string> = {
                  上岗提醒: "green",
                  "⚠️半班上岗": "orange",
                  缺编提醒: "red",
                  班次结束小结: "geekblue",
                };
                return <Tag color={colorMap[r.kind] || "default"}>{r.kind || "通知"}</Tag>;
              },
            },
            {
              title: "内容",
              width: 300,
              render: (_: unknown, r: PushLog) => (
                <span style={{ whiteSpace: "normal", fontSize: 12 }}>
                  {r.text || r.subject ||
                    (r.region || r.furnace ? `${r.region ?? ""}·${r.furnace ?? ""} ${r.squad ?? ""}`.trim() : "—")}
                </span>
              ),
            },
            {
              title: "接收人",
              width: 150,
              render: (_: unknown, r: PushLog) => {
                const nm = r.chiefName || r.targetName || "";
                const id = r.chiefEmpId || r.target || "";
                if (!nm && !id) return "—";
                return nm ? `${nm}${id ? `（${id}）` : ""}` : id;
              },
            },
            { title: "钉钉号", dataIndex: "ddUserId", width: 150, render: (d: string) => d || "—" },
            {
              title: "结果",
              width: 220,
              render: (_: unknown, r: PushLog) => <StatusTag status={r.status} reason={r.reason} />,
            },
          ]}
        />
      </Card>
    </>
  );
}
