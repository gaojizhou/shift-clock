import { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  Card,
  Input,
  Space,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import dayjs from "dayjs";
import { Link } from "react-router-dom";
import {
  getShiftPushConfig,
  getShiftPushRecipients,
  putShiftPushConfig,
  runShiftPush,
  type ShiftPushConfig,
  type ShiftPushRecipient,
} from "@fdc/api-client";
import { PushDebugSection } from "./DebugPush";
import { usePageCache } from "../pageCache";

export default function ShiftPushPage({ active = true }: { active?: boolean }) {
  const { invalidate } = usePageCache();
  const previousActive = useRef(active);
  const [cfg, setCfg] = useState<ShiftPushConfig | null>(null);
  const [recipients, setRecipients] = useState<ShiftPushRecipient[]>([]);
  const [newTime, setNewTime] = useState("");
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);

  const loadCfg = useCallback(() => {
    getShiftPushConfig().then(setCfg).catch(() => undefined);
    getShiftPushRecipients().then((r) => setRecipients(r.items)).catch(() => undefined);
  }, []);

  useEffect(() => { loadCfg(); }, [loadCfg]);

  useEffect(() => {
    const becameActive = active && !previousActive.current;
    previousActive.current = active;
    if (becameActive) {
      invalidate("/shift-push");
      loadCfg();
    }
  }, [active, invalidate, loadCfg]);

  const save = async (next: ShiftPushConfig) => {
    setSaving(true);
    try {
      const saved = await putShiftPushConfig(next);
      setCfg(saved);
      setRecipients((rs) => rs.map((r) => ({ ...r, enabled: !saved.excludedEmpIds.includes(r.empId) })));
      message.success("已保存");
    } catch {
      message.error("保存失败（需超管）");
    } finally {
      setSaving(false);
    }
  };

  const addTime = () => {
    const t = newTime.trim();
    if (!/^\d{1,2}:\d{2}$/.test(t)) { message.warning("时间格式 HH:MM"); return; }
    const hh = t.padStart(5, "0");
    if (!cfg) return;
    if (cfg.timePoints.includes(hh)) { message.info("该时间点已存在"); return; }
    void save({ ...cfg, timePoints: [...cfg.timePoints, hh].sort() });
    setNewTime("");
  };
  const removeTime = (t: string) => cfg && void save({ ...cfg, timePoints: cfg.timePoints.filter((x) => x !== t) });
  const toggleEnabled = (v: boolean) => cfg && void save({ ...cfg, enabled: v });
  const toggleRecipient = (empId: string, enabled: boolean) => {
    if (!cfg) return;
    const excluded = enabled
      ? cfg.excludedEmpIds.filter((x) => x !== empId)
      : [...new Set([...cfg.excludedEmpIds, empId])];
    void save({ ...cfg, excludedEmpIds: excluded });
  };

  const runNow = async (timePoint?: string) => {
    setRunning(true);
    try {
      const r = await runShiftPush({ timePoint, date: dayjs().format("YYYY-MM-DD") });
      const tot = r.results.reduce((a, b) => a + (b.records || 0), 0);
      message.success(`已推送 ${tot} 条`);
    } catch {
      message.error("推送失败（需超管）");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div>
      <h2>推送管理</h2>

      <Card
        title={
          <Space>
            <span>每日推送配置</span>
            <Switch
              checkedChildren="启用"
              unCheckedChildren="停用"
              checked={!!cfg?.enabled}
              loading={saving}
              onChange={toggleEnabled}
            />
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        <div style={{ marginBottom: 8, color: "#666" }}>
          推送时间点（命中地区时段结束时刻时，推全工作点当日小结；默认=各地区时段结束时刻）
        </div>
        <Space wrap style={{ marginBottom: 12 }}>
          {(cfg?.timePoints ?? []).map((t) => (
            <Tag key={t} closable onClose={(e) => { e.preventDefault(); removeTime(t); }} color="blue">
              {t}
            </Tag>
          ))}
          <Input
            size="small"
            placeholder="HH:MM"
            style={{ width: 90 }}
            value={newTime}
            onChange={(e) => setNewTime(e.target.value)}
            onPressEnter={addTime}
          />
          <Button size="small" onClick={addTime}>添加时间点</Button>
          <Button size="small" type="primary" loading={running} onClick={() => runNow()}>
            立即按全部时间点推送
          </Button>
        </Space>
      </Card>

      <Card title="收件人（值班长）· 默认全部，可取消" style={{ marginBottom: 16 }}>
        <Table
          size="small"
          rowKey="empId"
          pagination={false}
          dataSource={recipients}
          columns={[
            { title: "值班长", dataIndex: "name", render: (n, r) => `${n}（${r.empId}）` },
            {
              title: "钉钉号",
              dataIndex: "ddUserId",
              render: (d) => (d ? d : <Tag color="orange">无钉钉号·将记为未推</Tag>),
            },
            { title: "所辖工作点", dataIndex: "furnaces", render: (fs: string[]) => (fs || []).join("、") },
            {
              title: "接收推送",
              dataIndex: "enabled",
              width: 110,
              render: (en, r) => (
                <Switch
                  checked={en}
                  loading={saving}
                  onChange={(v) => toggleRecipient(r.empId, v)}
                />
              ),
            },
          ]}
        />
      </Card>

      {/* 工作点推送状态(测试推送) + 全部钉钉推送日志（含上岗提醒/缺编/当日小结/定时/测试，去除原重复的记录卡） */}
      <PushDebugSection />

      <nav
        aria-label="高级诊断工具"
        style={{
          marginTop: 40,
          paddingTop: 12,
          borderTop: "1px solid #f0f0f0",
          color: "#8c8c8c",
          fontSize: 12,
          textAlign: "right",
        }}
      >
        <span style={{ marginRight: 10 }}>高级诊断</span>
        <Space size={10} split={<span aria-hidden="true" style={{ color: "#d9d9d9" }}>·</span>}>
          <Link to="/dingtalk-load-test" style={{ color: "#8c8c8c" }}>钉钉压测</Link>
          <Link to="/connection-pool" style={{ color: "#8c8c8c" }}>连接池监控</Link>
        </Space>
      </nav>
    </div>
  );
}
