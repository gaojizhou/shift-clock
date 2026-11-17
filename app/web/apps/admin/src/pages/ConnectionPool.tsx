import { ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Tag,
  Typography,
  message,
} from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getConnectionPoolStatus,
  type ConnectionPoolStatus,
} from "@fdc/api-client";

const { Text, Paragraph } = Typography;

type TrendSample = {
  at: number;
  pool: number | null;
  database: number | null;
};

type StatusMeta = { label: string; color: string; alert: "success" | "warning" | "error" | "info" };
const STATUS_META: Record<
  ConnectionPoolStatus["status"],
  StatusMeta
> = {
  healthy: { label: "运行正常", color: "green", alert: "success" },
  warning: { label: "需要关注", color: "orange", alert: "warning" },
  critical: { label: "连接紧张", color: "red", alert: "error" },
  unavailable: { label: "指标不可用", color: "default", alert: "info" },
};

function formatTime(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function progressStatus(percent: number): "normal" | "exception" {
  return percent >= 90 ? "exception" : "normal";
}

function MiniTrend({ samples }: { samples: TrendSample[] }) {
  const width = 720;
  const height = 170;
  const left = 42;
  const right = 12;
  const top = 12;
  const bottom = 28;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const segments = (key: "pool" | "database") => {
    const result: string[][] = [];
    let current: string[] = [];
    samples.forEach((sample, index) => {
      const value = sample[key];
      if (value == null) {
        if (current.length) result.push(current);
        current = [];
      } else {
        const x = left + (samples.length <= 1 ? 0 : (index * chartWidth) / (samples.length - 1));
        const y = top + chartHeight - (Math.max(0, Math.min(100, value)) * chartHeight) / 100;
        current.push(`${x.toFixed(1)},${y.toFixed(1)}`);
      }
    });
    if (current.length) result.push(current);
    return result;
  };

  if (samples.length < 2) {
    return <div style={{ color: "#999", padding: "48px 0", textAlign: "center" }}>正在积累趋势数据…</div>;
  }

  return (
    <div style={{ width: "100%", overflowX: "auto" }}>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="最近连接使用率趋势" style={{ width: "100%", minWidth: 520 }}>
        {[0, 50, 70, 90, 100].map((value) => {
          const y = top + chartHeight - (value * chartHeight) / 100;
          const warningLine = value === 70 || value === 90;
          return (
            <g key={value}>
              <line
                x1={left}
                x2={width - right}
                y1={y}
                y2={y}
                stroke={warningLine ? "#ffd591" : "#f0f0f0"}
                strokeDasharray={warningLine ? "5 4" : undefined}
              />
              <text x={left - 7} y={y + 4} textAnchor="end" fontSize="11" fill="#8c8c8c">{value}%</text>
            </g>
          );
        })}
        {segments("pool").map((points, index) => (
          <polyline key={`pool-${index}`} points={points.join(" ")} fill="none" stroke="#1677ff" strokeWidth="2.5" />
        ))}
        {segments("database").map((points, index) => (
          <polyline key={`database-${index}`} points={points.join(" ")} fill="none" stroke="#52c41a" strokeWidth="2.5" />
        ))}
        <text x={left} y={height - 7} fontSize="11" fill="#8c8c8c">
          {new Date(samples[0].at).toLocaleTimeString("zh-CN", { hour12: false })}
        </text>
        <text x={width - right} y={height - 7} textAnchor="end" fontSize="11" fill="#8c8c8c">
          {new Date(samples[samples.length - 1].at).toLocaleTimeString("zh-CN", { hour12: false })}
        </text>
      </svg>
      <Space size="large" style={{ width: "100%", justifyContent: "center" }}>
        <Text><span style={{ color: "#1677ff" }}>■</span> 应用连接池</Text>
        <Text><span style={{ color: "#52c41a" }}>■</span> PostgreSQL</Text>
      </Space>
    </div>
  );
}

export default function ConnectionPoolPage() {
  const [data, setData] = useState<ConnectionPoolStatus | null>(null);
  const [history, setHistory] = useState<TrendSample[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [intervalSeconds, setIntervalSeconds] = useState(5);
  const inFlight = useRef(false);

  const load = useCallback(async (silent = false) => {
    if (inFlight.current) return;
    inFlight.current = true;
    if (!silent) setLoading(true);
    try {
      const next = await getConnectionPoolStatus();
      setData(next);
      setError("");
      setHistory((current) => [
        ...current,
        {
          at: Date.parse(next.generatedAt) || Date.now(),
          pool: next.pool.supported ? (next.pool.utilizationPercent ?? null) : null,
          database: next.database.supported ? (next.database.utilizationPercent ?? null) : null,
        },
      ].slice(-60));
    } catch (err) {
      const text = (err as Error).message || "连接池指标加载失败";
      setError(text);
      if (!silent) message.error(text);
    } finally {
      inFlight.current = false;
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(true);
    }, intervalSeconds * 1000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, intervalSeconds, load]);

  const statusKey = data?.status as string | undefined;
  const meta = (STATUS_META as Record<string, StatusMeta>)[statusKey ?? "unavailable"]
    ?? STATUS_META.unavailable;
  const poolPercent = data?.pool.utilizationPercent ?? 0;
  const databasePercent = data?.database.utilizationPercent ?? 0;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card
        title={
          <Space>
            <span>连接池监控</span>
            {data && <Tag color={meta.color}>{meta.label}</Tag>}
          </Space>
        }
        extra={
          <Space wrap>
            <Text type="secondary">更新时间：{formatTime(data?.generatedAt)}</Text>
            <Space size={6}>
              <Text>自动刷新</Text>
              <Switch size="small" checked={autoRefresh} onChange={setAutoRefresh} />
            </Space>
            <Select
              size="small"
              value={intervalSeconds}
              disabled={!autoRefresh}
              style={{ width: 88 }}
              onChange={setIntervalSeconds}
              options={[
                { value: 5, label: "5 秒" },
                { value: 10, label: "10 秒" },
                { value: 30, label: "30 秒" },
              ]}
            />
            <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
          </Space>
        }
      >
        {error && <Alert type="error" showIcon message="监控请求失败" description={error} style={{ marginBottom: 16 }} />}
        {data && (
          <Alert
            showIcon
            type={meta.alert}
            message={`当前状态：${meta.label}`}
            description={
              data.status === "healthy"
                ? "应用连接池与 PostgreSQL 连接均处于安全阈值内。"
                : data.status === "warning"
                  ? "连接使用率、数据库锁等待、空闲事务或审计队列已达到关注条件，请结合趋势继续观察。"
                  : data.status === "critical"
                    ? "连接容量接近耗尽，请立即检查慢请求、同步任务和数据库等待。"
                    : "当前环境无法提供完整连接指标。"
            }
          />
        )}
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}>
          <Card title="应用连接池（当前 API 进程）" style={{ height: "100%" }}>
            {!data?.pool.supported ? (
              <Alert type="info" showIcon message={data?.pool.reason || "连接池指标不可用"} />
            ) : (
              <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                <div>
                  <Space style={{ width: "100%", justifyContent: "space-between" }}>
                    <Text>已借出连接 / 最大容量</Text>
                    <Text strong>{data.pool.checkedOut} / {data.pool.maxCapacity}</Text>
                  </Space>
                  <Progress
                    percent={poolPercent}
                    status={progressStatus(poolPercent)}
                    strokeColor={poolPercent >= 70 ? "#fa8c16" : "#1677ff"}
                  />
                </div>
                <Row gutter={[12, 16]}>
                  <Col span={8}><Statistic title="已借出" value={data.pool.checkedOut ?? 0} /></Col>
                  <Col span={8}><Statistic title="池内空闲" value={data.pool.checkedIn ?? 0} /></Col>
                  <Col span={8}><Statistic title="当前溢出" value={data.pool.currentOverflow ?? 0} /></Col>
                  <Col span={8}><Statistic title="常驻容量" value={data.pool.poolSize ?? 0} /></Col>
                  <Col span={8}><Statistic title="突发容量" value={data.pool.maxOverflow ?? 0} /></Col>
                  <Col span={8}>
                    <Statistic
                      title="等待超时"
                      value={data.pool.timeoutSeconds ?? "—"}
                      suffix={data.pool.timeoutSeconds == null ? undefined : "秒"}
                    />
                  </Col>
                </Row>
                <Text type="secondary">
                  Pool：{data.pool.poolClass} · 进程 PID：{data.process.pid}。已借出通常包含当前监控请求自身。
                </Text>
              </Space>
            )}
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card title="PostgreSQL 服务器连接" style={{ height: "100%" }}>
            {!data?.database.supported ? (
              <Alert type="info" showIcon message={data?.database.reason || "数据库会话指标不可用"} />
            ) : (
              <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                <div>
                  <Space style={{ width: "100%", justifyContent: "space-between" }}>
                    <Text>服务器连接 / 普通连接可用额度</Text>
                    <Text strong>{data.database.serverTotal} / {data.database.availableBudget ?? data.database.maxConnections}</Text>
                  </Space>
                  <Progress
                    percent={databasePercent}
                    status={progressStatus(databasePercent)}
                    strokeColor={databasePercent >= 75 ? "#fa8c16" : "#52c41a"}
                  />
                </div>
                <Row gutter={[12, 16]}>
                  <Col span={8}><Statistic title="当前数据库" value={data.database.databaseTotal ?? 0} /></Col>
                  <Col span={8}><Statistic title="活跃" value={data.database.active ?? 0} /></Col>
                  <Col span={8}><Statistic title="空闲" value={data.database.idle ?? 0} /></Col>
                  <Col span={8}><Statistic title="数据库锁等待" value={data.database.lockWaiting ?? 0} /></Col>
                  <Col span={8}><Statistic title="事务内空闲" value={data.database.idleInTransaction ?? 0} /></Col>
                  <Col span={8}><Statistic title="最长事务" value={data.database.longestTransactionSeconds ?? 0} suffix="秒" precision={1} /></Col>
                </Row>
                <Text type="secondary">
                  PostgreSQL 为整个服务器口径；预留连接 {data.database.reservedConnections ?? 0} 个，
                  普通连接剩余 {data.database.remainingConnections ?? "—"} 个。
                </Text>
              </Space>
            )}
          </Card>
        </Col>
      </Row>

      {data?.reports && (
        <Card title="报表计算">
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Alert showIcon type={data.reports.paused ? "warning" : "info"}
              message={data.reports.paused ? "报表维护暂停中" : `最多同时计算 ${data.reports.capacity} 个报表，超出时提示稍后重试`} />
            <Row gutter={[12, 16]}>
              <Col xs={12} md={6}><Statistic title="计算中" value={data.reports.active} /></Col>
              <Col xs={12} md={6}><Statistic title="API 进程内存" value={data.reports.residentMemoryMb ?? "—"} suffix="MiB" /></Col>
              <Col xs={12} md={6}><Statistic title="最近耗时 P95" value={data.reports.recentP95Ms ?? "—"} suffix="ms" /></Col>
              <Col xs={12} md={6}><Statistic title="完成 / 失败" value={`${data.reports.completed} / ${data.reports.failed}`} /></Col>
              <Col xs={12} md={6}><Statistic title="拒绝" value={data.reports.rejected} /></Col>
              <Col xs={12} md={6}><Statistic title="合并重复请求" value={data.reports.coalesced} /></Col>
            </Row>
            {data.reports.inFlight.map((job, i) => (
              <Text key={`${job.endpoint}-${i}`}>{job.endpoint}：已运行 {job.seconds} 秒，等待相同结果 {job.followers} 人</Text>
            ))}
            <Text type="secondary">
              指标和并发限制均为当前 API 进程口径，重启后计数归零。P95 取最近 {data.reports.recentCount} 次计算；
              超时预算 {data.reports.timeoutSeconds} 秒，源数据累计上限 {data.reports.maxSourceRows} 行，Excel 上限 {data.reports.maxOutputCells} 个单元格。
            </Text>
          </Space>
        </Card>
      )}

      {data?.requestAuditQueue?.enabled && (
        <Card title="请求审计缓冲队列">
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Alert
              showIcon
              type={
                data.requestAuditQueue.available
                && data.requestAuditQueue.streamHealth?.workerHealthy !== false
                  ? "success"
                  : "warning"
              }
              message={
                data.requestAuditQueue.available
                && data.requestAuditQueue.streamHealth?.workerHealthy !== false
                  ? "Redis 与审计 Worker 正常"
                  : "审计队列正在降级或 Worker 心跳异常"
              }
              description={data.requestAuditQueue.lastError || undefined}
            />
            <Row gutter={[12, 16]}>
              <Col xs={12} md={6}><Statistic title="内存积压" value={data.requestAuditQueue.memoryQueue.pending} /></Col>
              <Col xs={12} md={6}><Statistic title="内存使用率" value={data.requestAuditQueue.memoryQueue.utilizationPercent} suffix="%" /></Col>
              <Col xs={12} md={6}><Statistic title="Stream 长度" value={data.requestAuditQueue.streamHealth?.length ?? "—"} /></Col>
              <Col xs={12} md={6}><Statistic title="消费者 Lag" value={data.requestAuditQueue.streamHealth?.consumerLag ?? "—"} /></Col>
              <Col xs={12} md={6}><Statistic title="成功入 Redis" value={data.requestAuditQueue.enqueuedSinceStart} /></Col>
              <Col xs={12} md={6}><Statistic title="溢出" value={data.requestAuditQueue.overflowedSinceStart} /></Col>
              <Col xs={12} md={6}><Statistic title="Redis 重试批次" value={data.requestAuditQueue.redisWriteFailuresSinceStart} /></Col>
              <Col xs={12} md={6}><Statistic title="同步安全兜底" value={data.requestAuditQueue.redisUnavailableFallbacksSinceStart} /></Col>
            </Row>
          </Space>
        </Card>
      )}

      <Card title={`最近趋势（本页面内存，最多 ${history.length}/60 个采样点）`}>
        <MiniTrend samples={history} />
      </Card>

      <Card size="small" title="口径说明">
        <Paragraph style={{ marginBottom: 0 }}>
          {data?.notes.map((note) => <div key={note}>• {note}</div>)}
          <div>• 黄色虚线对应 70%/90% 关注线；页面隐藏或离开后自动停止刷新。</div>
          <div>• 将 Uvicorn 增加到多个 Worker 后，每个 Worker 都有独立连接池，本页应用指标只代表命中当前请求的进程。</div>
        </Paragraph>
      </Card>
    </Space>
  );
}
