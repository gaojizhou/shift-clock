import { ThunderboltOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  InputNumber,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  getAdminEmployees,
  runDingtalkUserFetchLoadTest,
  type DingtalkUserFetchLoadTestResult,
  type DingtalkUserFetchLoadTestResultItem,
} from "@fdc/api-client";

const { Paragraph, Text } = Typography;
const SAFE_CONCURRENCY = 10;
const USER_GET_REQUESTS_PER_FIRST_LOGIN = 1;

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${milliseconds.toFixed(1)} ms`;
  return `${(milliseconds / 1000).toFixed(2)} 秒`;
}

export default function DingtalkLoadTestPage() {
  const [userids, setUserids] = useState<string[]>([]);
  const [requestCount, setRequestCount] = useState(10);
  const [concurrency, setConcurrency] = useState(2);
  const [employeeOptions, setEmployeeOptions] = useState<
    { value: string; label: string }[]
  >([]);
  const [loadingEmployees, setLoadingEmployees] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<DingtalkUserFetchLoadTestResult | null>(null);

  useEffect(() => {
    let current = true;
    setLoadingEmployees(true);
    getAdminEmployees()
      .then((response) => {
        if (!current) return;
        const seen = new Set<string>();
        setEmployeeOptions(response.items.flatMap((employee) => {
          const userid = employee.ddUserId?.trim();
          if (!userid || seen.has(userid)) return [];
          seen.add(userid);
          return [{
            value: userid,
            label: `${employee.name} · ${employee.empId} · ${userid}`,
          }];
        }));
      })
      .catch((error) => {
        if (current) message.error((error as Error).message || "员工钉钉账号加载失败");
      })
      .finally(() => {
        if (current) setLoadingEmployees(false);
      });
    return () => { current = false; };
  }, []);

  const selectedUserids = useMemo(
    () => [...new Set(userids.map((value) => value.trim()).filter(Boolean))].slice(0, 20),
    [userids],
  );
  const invalidConcurrency = concurrency > requestCount;

  const run = async () => {
    if (!selectedUserids.length) {
      message.warning("请至少选择或输入一个钉钉 userid");
      return;
    }
    if (invalidConcurrency) {
      message.warning("并发数不能大于请求总数");
      return;
    }
    setRunning(true);
    setResult(null);
    try {
      const next = await runDingtalkUserFetchLoadTest({
        userids: selectedUserids,
        requestCount,
        concurrency,
        confirmHighLoad: concurrency > SAFE_CONCURRENCY,
      });
      setResult(next);
      if (next.errorCount) {
        message.warning(`压测完成：${next.errorCount} 次失败`);
      } else {
        message.success("压测完成，全部请求成功");
      }
    } catch (error) {
      const value = error as { message?: string; data?: { detail?: string } };
      message.error(value.data?.detail || value.message || "压测执行失败");
    } finally {
      setRunning(false);
    }
  };

  const columns = [
    { title: "序号", dataIndex: "index", width: 72 },
    { title: "钉钉 userid", dataIndex: "userid", ellipsis: true },
    {
      title: "结果",
      dataIndex: "ok",
      width: 90,
      render: (ok: boolean, row: DingtalkUserFetchLoadTestResultItem) => (
        <Tag color={ok ? "green" : row.rateLimited ? "orange" : "red"}>
          {ok ? "成功" : row.rateLimited ? "限流" : "失败"}
        </Tag>
      ),
    },
    { title: "耗时", dataIndex: "latencyMs", width: 110, render: (value: number) => `${value.toFixed(1)} ms` },
    { title: "姓名", dataIndex: "name", width: 120, render: (value: string | null) => value || "—" },
    { title: "职位", dataIndex: "title", width: 140, render: (value: string | null) => value || "—" },
    { title: "部门数", dataIndex: "departmentCount", width: 90, render: (value: number | null) => value ?? "—" },
    {
      title: "错误",
      dataIndex: "error",
      ellipsis: true,
      render: (value: string | null, row: DingtalkUserFetchLoadTestResultItem) => (
        value ? `${row.errcode == null ? "" : `[${row.errcode}] `}${value}` : "—"
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card title="钉钉信息获取压测">
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Alert
            showIcon
            type="warning"
            message="该功能会真实调用钉钉接口"
            description="仅管理员可执行；只读取 user/get，不写员工表、收藏或工作点绑定。建议先用 10 次、并发 2 做基线，再逐步加压。"
          />
          <Alert
            showIcon
            type="info"
            message="用户等效口径"
            description={`优化后的首次登录约产生 ${USER_GET_REQUESTS_PER_FIRST_LOGIN} 次 user/get 和 1 次 getuserinfo；所以本页请求总数约等于相同数量新用户的 user/get 调用量。并发数表示同一瞬间并行读取信息的近似用户数，不包含额外的部门路径查询。`}
          />
          <div>
            <Text strong>测试账号</Text>
            <Select
              mode="tags"
              allowClear
              showSearch
              loading={loadingEmployees}
              optionFilterProp="label"
              tokenSeparators={[",", "，", " "]}
              maxCount={20}
              value={userids}
              options={employeeOptions}
              onChange={setUserids}
              placeholder="选择员工，或直接粘贴钉钉 userid；最多 20 个"
              style={{ width: "100%", marginTop: 8 }}
            />
            <Text type="secondary">请求会按所选账号轮询分配，重复读取不会修改钉钉或本系统数据。</Text>
          </div>
          <Space wrap size="large">
            <Space>
              <Text strong>请求总数</Text>
              <InputNumber
                min={1}
                max={100}
                value={requestCount}
                onChange={(value) => setRequestCount(value ?? 1)}
              />
            </Space>
            <Space>
              <Text strong>并发数</Text>
              <InputNumber
                min={1}
                max={50}
                status={invalidConcurrency ? "error" : undefined}
                value={concurrency}
                onChange={(value) => setConcurrency(value ?? 1)}
              />
            </Space>
            <Popconfirm
              title="确认开始真实钉钉接口压测？"
              description={`将执行 ${requestCount} 次请求，并发 ${concurrency}；压测开始后不能中止。`}
              okText="开始压测"
              cancelText="取消"
              disabled={!selectedUserids.length || invalidConcurrency || running}
              onConfirm={() => void run()}
            >
              <Button
                type="primary"
                icon={<ThunderboltOutlined />}
                loading={running}
                disabled={!selectedUserids.length || invalidConcurrency}
              >
                {running ? "压测执行中…" : "开始压测"}
              </Button>
            </Popconfirm>
          </Space>
          {concurrency >= SAFE_CONCURRENCY && (
            <Alert
              type={concurrency >= 30 ? "error" : "warning"}
              showIcon
              message={
                concurrency >= 30
                  ? `当前并发 ${concurrency} 属于极限加压，可能影响共享钉钉接口配额`
                  : `当前并发 ${concurrency} 较高，可能触发钉钉 90002 限流`
              }
            />
          )}
        </Space>
      </Card>

      {result && (
        <>
          <Card title="压测结果">
            <Row gutter={[16, 16]}>
              <Col xs={12} md={6}><Statistic title="成功率" value={result.successRate} suffix="%" /></Col>
              <Col xs={12} md={6}><Statistic title="吞吐量" value={result.throughputRps} suffix="次/秒" precision={2} /></Col>
              <Col xs={12} md={6}><Statistic title="P95" value={result.latencyMs.p95} suffix="ms" precision={1} /></Col>
              <Col xs={12} md={6}><Statistic title="P99" value={result.latencyMs.p99} suffix="ms" precision={1} /></Col>
              <Col xs={12} md={6}><Statistic title="总耗时" value={formatDuration(result.durationMs)} /></Col>
              <Col xs={12} md={6}><Statistic title="成功 / 失败" value={`${result.successCount} / ${result.errorCount}`} /></Col>
              <Col xs={12} md={6}><Statistic title="钉钉限流" value={result.rateLimitedCount} suffix="次" /></Col>
              <Col xs={12} md={6}><Statistic title="实际峰值并发（约等效同时用户）" value={result.peakConcurrency} /></Col>
              <Col xs={12} md={6}>
                <Statistic
                  title="约等效首次用户总量"
                  value={result.requestCount / USER_GET_REQUESTS_PER_FIRST_LOGIN}
                  suffix="人"
                  precision={1}
                />
              </Col>
            </Row>
            <Progress
              percent={result.successRate}
              status={result.errorCount ? "exception" : "success"}
              style={{ marginTop: 18 }}
            />
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              延迟：最小 {result.latencyMs.min} ms · 平均 {result.latencyMs.average} ms ·
              P50 {result.latencyMs.p50} ms · 最大 {result.latencyMs.max} ms
            </Paragraph>
          </Card>

          <Card title={`请求明细（${result.requestCount} 次）`}>
            <Table<DingtalkUserFetchLoadTestResultItem>
              rowKey="index"
              size="small"
              columns={columns}
              dataSource={result.results}
              pagination={{ pageSize: 10, showSizeChanger: true }}
              scroll={{ x: 1050 }}
            />
          </Card>

          <Card size="small" title="结果口径">
            <Paragraph style={{ marginBottom: 0 }}>
              {result.notes.map((note) => <div key={note}>• {note}</div>)}
            </Paragraph>
          </Card>
        </>
      )}
    </Space>
  );
}
