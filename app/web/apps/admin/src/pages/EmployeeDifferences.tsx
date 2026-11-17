import { useCallback, useEffect, useMemo, useRef, useState, type Key } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Input,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import {
  addEmployeeSyncWhitelist,
  applyDingtalkEmployeeDifferences,
  getAdminEmployees,
  getDingtalkEmployeeSyncStatus,
  getEmployeeAutoSyncConfig,
  getEmployeeManualSyncStatus,
  getEmployeeSyncWhitelist,
  previewDingtalkEmployeeDifferences,
  putEmployeeAutoSyncConfig,
  removeEmployeeSyncWhitelist,
  startDingtalkEmployeeSync,
  startEmployeeManualSync,
  type EmployeeAutoSyncConfig,
  type EmployeeDifferenceType,
  type EmployeeSyncField,
  type EmployeeSyncPreview,
  type EmployeeSyncPreviewItem,
  type EmployeeSyncRule,
  type EmployeeSyncUpdateMode,
  type EmployeeSyncWhitelistItem,
} from "@fdc/api-client";

const FIELD_LABEL: Record<EmployeeSyncField, string> = {
  region: "地区",
  role: "系统角色",
  post: "常用职位",
};
const SCOPE_OPTIONS = [
  { value: "local_empty", label: "仅本地为空" },
  { value: "different", label: "仅存在差异" },
  { value: "mapped", label: "仅远程已配置映射" },
  { value: "syncable", label: "全部可同步人员" },
];
const DIFF_OPTIONS: { value: EmployeeDifferenceType; label: string }[] = [
  { value: "empty_remote", label: "本地为空、远程有值" },
  { value: "contains_remote", label: "本地已有且包含远程值" },
  { value: "missing_remote", label: "本地已有但不包含远程值" },
  { value: "multi", label: "本地多值" },
];
const MODE_LABEL: Record<EmployeeSyncUpdateMode, string> = {
  append: "追加",
  fill_empty: "仅填空",
  replace: "替换（高风险）",
};

function defaultRule(field: EmployeeSyncField): EmployeeSyncRule {
  return {
    field,
    enabled: true,
    scope: "different",
    updateMode: field === "role" ? "fill_empty" : "append",
    q: "",
    localRegions: [],
    currentRoles: [],
    targetRoles: [],
    remoteTitles: [],
    departments: [],
    furnaces: [],
    differenceTypes: ["empty_remote", "missing_remote"],
    skipMulti: true,
  };
}

function ruleSignature(rule: EmployeeSyncRule | null | undefined) {
  if (!rule) return "";
  const normalized = (values: string[]) => [
    ...new Set(values.map((value) => value.trim()).filter(Boolean)),
  ];
  return JSON.stringify({
    field: rule.field,
    scope: rule.scope,
    updateMode: rule.updateMode,
    q: rule.q.trim(),
    localRegions: normalized(rule.localRegions),
    currentRoles: normalized(rule.currentRoles),
    targetRoles: normalized(rule.targetRoles),
    remoteTitles: normalized(rule.remoteTitles),
    departments: normalized(rule.departments),
    furnaces: normalized(rule.furnaces),
    differenceTypes: normalized(rule.differenceTypes),
    skipMulti: rule.skipMulti,
  });
}

function displayValue(value: string | string[] | null) {
  if (Array.isArray(value)) return value.length ? value.join("、") : "（空）";
  return value || "（空）";
}

function multiOptions(values: string[] | undefined) {
  return (values ?? []).map((value) => ({ value, label: value }));
}

function pollDelay(attempt: number) {
  return Math.min(1000 * (1.5 ** attempt), 5000);
}

function abortableDelay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("轮询已取消", "AbortError"));
      return;
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, ms);
    const abort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("轮询已取消", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

function PreviewChange({ item }: { item: EmployeeSyncPreviewItem }) {
  const color = item.action === "replace" ? "red" : item.action === "skip" ? "default" : "blue";
  return (
    <div style={{ lineHeight: 1.7 }}>
      <Tag color={color}>{{
        fill: "填空",
        append: "追加",
        replace: "替换",
        unchanged: "无变化",
        skip: "跳过",
      }[item.action]}</Tag>
      {!!item.willAdd.length && <div style={{ color: "#1677ff" }}>增加：{item.willAdd.join("、")}</div>}
      {!!item.willRemove.length && <div style={{ color: "#cf1322" }}>删除：{item.willRemove.join("、")}</div>}
      {item.skipReason && <div style={{ color: "#999" }}>{item.skipReason}</div>}
    </div>
  );
}

export default function EmployeeDifferencesPage() {
  const [field, setField] = useState<EmployeeSyncField>("region");
  const [rule, setRule] = useState<EmployeeSyncRule>(() => defaultRule("region"));
  const [preview, setPreview] = useState<EmployeeSyncPreview | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(100);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [selected, setSelected] = useState<Key[]>([]);
  const [config, setConfig] = useState<EmployeeAutoSyncConfig | null>(null);
  const [savingConfig, setSavingConfig] = useState(false);
  const [whitelist, setWhitelist] = useState<EmployeeSyncWhitelistItem[]>([]);
  const [whitelistCandidates, setWhitelistCandidates] = useState<{ value: string; label: string }[]>([]);
  const [whitelistSelected, setWhitelistSelected] = useState<string[]>([]);
  const previewRequestId = useRef(0);
  const blockedSelection = useRef(new Set<string>());
  const mounted = useRef(true);
  const currentField = useRef(field);
  const pollControllers = useRef(new Set<AbortController>());
  currentField.current = field;
  const previewCurrent = useMemo(
    () => Boolean(preview && ruleSignature(preview.rule) === ruleSignature(rule)),
    [preview, rule],
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      pollControllers.current.forEach((controller) => controller.abort());
      pollControllers.current.clear();
    };
  }, []);

  const loadConfig = useCallback(async () => {
    const value = await getEmployeeAutoSyncConfig();
    if (!mounted.current) return;
    const activeField = currentField.current;
    setConfig(value);
    setRule({ ...value.rules[activeField], field: activeField, enabled: true });
  }, []);

  const loadWhitelist = useCallback(async () => {
    const value = await getEmployeeSyncWhitelist();
    if (mounted.current) setWhitelist(value.items);
  }, []);

  useEffect(() => {
    void Promise.all([loadConfig(), loadWhitelist()]).catch((error) =>
      message.error((error as Error).message || "加载同步配置失败"),
    );
  }, [loadConfig, loadWhitelist]);

  const loadPreview = useCallback(async () => {
    const requestId = ++previewRequestId.current;
    setLoading(true);
    try {
      const value = await previewDingtalkEmployeeDifferences(rule, page, pageSize);
      if (requestId !== previewRequestId.current) return;
      value.items.forEach((item) => {
        if (item.skipReason || !item.different) blockedSelection.current.add(item.empId);
        else blockedSelection.current.delete(item.empId);
      });
      setPreview(value);
    } catch (error) {
      if (requestId === previewRequestId.current && mounted.current) {
        setPreview(null);
        message.error((error as Error).message || "加载员工差异预览失败");
      }
    } finally {
      if (requestId === previewRequestId.current) setLoading(false);
    }
  }, [rule, page, pageSize]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadPreview(), 350);
    return () => window.clearTimeout(timer);
  }, [loadPreview]);

  const patchRule = <K extends keyof EmployeeSyncRule>(key: K, value: EmployeeSyncRule[K]) => {
    setPage(1);
    // 改筛选/范围/更新方式/搜索会改变预览集合，已选工号可能不再可见，需清空避免误提交隐藏行。
    setSelected([]);
    blockedSelection.current.clear();
    setRule((current) => ({ ...current, [key]: value }));
  };

  const changeField = (next: EmployeeSyncField) => {
    setField(next);
    setPage(1);
    setSelected([]);
    blockedSelection.current.clear();
    setRule({ ...(config?.rules[next] ?? defaultRule(next)), field: next, enabled: true });
  };

  const waitDirectoryPull = async (signal: AbortSignal) => {
    const started = await startDingtalkEmployeeSync();
    if (!started.started) throw new Error("通讯录任务正在执行");
    const deadline = Date.now() + 10 * 60 * 1000;
    for (let attempt = 0; Date.now() < deadline; attempt += 1) {
      await abortableDelay(pollDelay(attempt), signal);
      const status = await getDingtalkEmployeeSyncStatus();
      if (status.status === "done") return status;
      if (status.status === "error") throw new Error(status.message || "通讯录拉取失败");
    }
    throw new Error("通讯录拉取超时，请稍后查看状态");
  };

  const pullLatest = async () => {
    const controller = new AbortController();
    pollControllers.current.add(controller);
    setPulling(true);
    try {
      await waitDirectoryPull(controller.signal);
      if (!mounted.current) return;
      message.success("最新通讯录拉取完成");
      await loadPreview();
    } catch (error) {
      if (!isAbortError(error) && mounted.current) {
        message.error((error as Error).message);
      }
    } finally {
      pollControllers.current.delete(controller);
      if (mounted.current) setPulling(false);
    }
  };

  const runManual = async () => {
    if (rule.field !== field || !previewCurrent || loading) {
      message.warning(loading ? "请等待当前条件的预览刷新完成" : "当前条件与预览不一致，请刷新预览后重试");
      return;
    }
    const controller = new AbortController();
    pollControllers.current.add(controller);
    setApplying(true);
    try {
      const started = await startEmployeeManualSync({ ...rule, enabled: true });
      const deadline = Date.now() + 15 * 60 * 1000;
      for (let attempt = 0; Date.now() < deadline; attempt += 1) {
        await abortableDelay(pollDelay(attempt), controller.signal);
        const status = await getEmployeeManualSyncStatus(started.id);
        if (status.status === "done") {
          if (!mounted.current) return;
          const result = status.result?.fields[0];
          message.success(
            `同步完成：更新 ${result?.changed ?? 0}，无变化 ${result?.unchanged ?? 0}，跳过 ${result?.skipped ?? 0}`,
            8,
          );
          await Promise.all([loadPreview(), loadWhitelist()]);
          return;
        }
        if (status.status === "error") throw new Error(status.message || "一键同步失败");
      }
      throw new Error("同步任务超时，请稍后查看状态");
    } catch (error) {
      if (!isAbortError(error) && mounted.current) {
        message.error((error as Error).message);
      }
    } finally {
      pollControllers.current.delete(controller);
      if (mounted.current) setApplying(false);
    }
  };

  const applySelected = async () => {
    if (!previewCurrent || loading) {
      message.warning(loading ? "请等待当前条件的预览刷新完成" : "当前条件与预览不一致，请刷新预览后重试");
      return;
    }
    const eligible = selected.filter((key) => !blockedSelection.current.has(String(key)));
    if (!eligible.length) {
      message.warning("所选员工在最近一次预览后均已不可同步，请重新勾选");
      return;
    }
    if (eligible.length > 500) {
      message.warning(`单次最多同步 500 人，当前可同步 ${eligible.length} 人，请分批选择`);
      return;
    }
    setApplying(true);
    try {
      const result = await applyDingtalkEmployeeDifferences({
        empIds: eligible.map(String),
        field,
        // 透传当前更新方式，与预览「替换/追加/仅填空」语义保持一致。
        updateMode: rule.updateMode,
        rule: { ...rule, enabled: true },
      });
      message.success(`更新 ${result.changed}，无变化 ${result.unchanged}，跳过 ${result.skipped}`);
      setSelected([]);
      await loadPreview();
    } catch (error) {
      message.error((error as Error).message || "同步失败");
    } finally {
      setApplying(false);
    }
  };

  const applyOne = async (empId: string) => {
    if (!previewCurrent || loading) {
      message.warning(loading ? "请等待当前条件的预览刷新完成" : "当前条件与预览不一致，请刷新预览后重试");
      return;
    }
    setApplying(true);
    try {
      const result = await applyDingtalkEmployeeDifferences({
        empIds: [empId],
        field,
        updateMode: rule.updateMode,
        rule: { ...rule, enabled: true },
      });
      if (result.changed) message.success("同步完成");
      else message.info(result.skippedItems[0]?.reason || "没有变化");
      await loadPreview();
    } catch (error) {
      message.error((error as Error).message || "同步失败");
      // 失败后刷新预览回到真实状态，避免行内仍显示旧差异。
      await loadPreview();
    } finally {
      setApplying(false);
    }
  };

  const saveCurrentToAuto = () => {
    if (rule.q.trim()) {
      message.warning(
        "姓名或工号仅用于人工定位，不能静默剥离后保存为无人值守规则；请先清空该筛选并重新预览",
      );
      return;
    }
    const finalMode = rule.updateMode === "replace"
      ? defaultRule(field).updateMode
      : rule.updateMode;
    setConfig((current) => current ? {
      ...current,
      rules: {
        ...current.rules,
        // 姓名/工号非空时已在上方阻止保存；高风险 replace 降为安全默认值。
        // append/fill_empty 保留用户刚预览过的语义。
        [field]: {
          ...rule,
          q: "",
          updateMode: finalMode,
          enabled: current.rules[field].enabled,
        },
      },
    } : current);
    message.success(
      `已将当前条件放入${FIELD_LABEL[field]}自动规则，更新方式：${MODE_LABEL[finalMode]}；请点击“保存自动配置”生效`,
    );
  };

  const saveAutoConfig = async () => {
    if (!config) return;
    setSavingConfig(true);
    try {
      setConfig(await putEmployeeAutoSyncConfig({
        enabled: config.enabled,
        rules: config.rules,
      }));
      message.success("自动同步配置已保存");
    } catch (error) {
      message.error((error as Error).message || "保存失败");
    } finally {
      setSavingConfig(false);
    }
  };

  const searchWhitelistCandidates = async (value: string) => {
    if (!value.trim()) return;
    try {
      const result = await getAdminEmployees({ q: value.trim() });
      setWhitelistCandidates(result.items.slice(0, 50).map((employee) => ({
        value: employee.empId,
        label: `${employee.name} · ${employee.empId} · ${employee.role}`,
      })));
    } catch (error) {
      message.error((error as Error).message || "搜索白名单候选人失败");
    }
  };

  const addWhitelist = async () => {
    if (!whitelistSelected.length) return;
    try {
      setWhitelist((await addEmployeeSyncWhitelist(whitelistSelected)).items);
      setWhitelistSelected([]);
      await loadPreview();
    } catch (error) {
      message.error((error as Error).message || "加入白名单失败");
    }
  };

  const removeWhitelist = async (empId: string) => {
    try {
      setWhitelist((await removeEmployeeSyncWhitelist(empId)).items);
      await loadPreview();
    } catch (error) {
      message.error((error as Error).message || "移出白名单失败");
    }
  };

  const modeOptions: { value: EmployeeSyncUpdateMode; label: string }[] =
    field === "role"
      ? [
        { value: "fill_empty", label: "仅填空" },
        { value: "replace", label: "替换原值（高风险）" },
      ]
      : [
        { value: "append", label: "仅追加，不删除已有值" },
        { value: "fill_empty", label: "仅填空" },
        { value: "replace", label: "替换原值（高风险）" },
      ];

  const options = preview?.options;
  const skipReasonText = useMemo(
    () => Object.entries(preview?.skipReasons ?? {}).map(([reason, count]) => `${reason} ${count}`).join("；"),
    [preview],
  );

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="员工差异只同步地区、系统角色和常用职位。白名单及 admin/HR/统计受服务端强制保护。"
      />

      <Card title="同步条件与操作前预览">
        <Row gutter={[12, 12]}>
          <Col span={6}>
            <div>同步字段</div>
            <Select value={field} onChange={changeField} style={{ width: "100%" }}
              options={Object.entries(FIELD_LABEL).map(([value, label]) => ({ value, label }))} />
          </Col>
          <Col span={6}>
            <div>同步范围</div>
            <Select value={rule.scope} onChange={(value) => patchRule("scope", value)}
              style={{ width: "100%" }} options={SCOPE_OPTIONS} />
          </Col>
          <Col span={6}>
            <div>更新方式</div>
            <Select value={rule.updateMode} onChange={(value) => patchRule("updateMode", value)}
              style={{ width: "100%" }} options={modeOptions} />
          </Col>
          <Col span={6}>
            <div>姓名或工号</div>
            <Input value={rule.q} onChange={(event) => patchRule("q", event.target.value)}
              allowClear placeholder="输入姓名或工号" />
          </Col>
          {([
            ["localRegions", "地区", options?.localRegions],
            ["currentRoles", "当前系统角色", options?.currentRoles],
            ["targetRoles", "远程目标角色", options?.targetRoles],
            ["remoteTitles", "钉钉职位", options?.remoteTitles],
            ["departments", "部门/路径", options?.departments],
            ["furnaces", "工作点", options?.furnaces],
          ] as const).map(([key, label, values]) => (
            <Col span={8} key={key}>
              <div>{label}</div>
              <Select mode="multiple" allowClear value={rule[key]}
                onChange={(value) => patchRule(key, value)}
                style={{ width: "100%" }} options={multiOptions(values)} />
            </Col>
          ))}
          <Col span={24}>
            <div>差异类型</div>
            <Checkbox.Group
              value={rule.differenceTypes}
              onChange={(value) => {
                const allowed = DIFF_OPTIONS
                  .map((option) => option.value)
                  .filter((item) => field !== "role" || item !== "multi");
                const selectedTypes = value.filter(
                  (item) => field !== "role" || item !== "multi",
                ) as EmployeeDifferenceType[];
                if (!selectedTypes.length) {
                  patchRule("differenceTypes", allowed);
                  message.info("差异类型不能全空，已恢复为全选（表示不限差异类型）");
                  return;
                }
                patchRule("differenceTypes", selectedTypes);
              }}
              options={DIFF_OPTIONS.map((option) => ({
                ...option,
                disabled: field === "role" && option.value === "multi",
              }))}
            />
            <span style={{ marginLeft: 8, color: "#999", fontSize: 12 }}>
              全选表示不限差异类型；取消最后一项时会恢复全选
            </span>
          </Col>
          <Col span={24}>
            <Space wrap>
              <Checkbox checked disabled>跳过 admin、HR、统计</Checkbox>
              <Checkbox checked={rule.skipMulti}
                onChange={(event) => patchRule("skipMulti", event.target.checked)}>
                跳过存在多个地区或职位
              </Checkbox>
              <Checkbox checked disabled>跳过未配置映射</Checkbox>
              <Checkbox checked disabled>跳过远程离职</Checkbox>
              <Checkbox checked disabled>跳过超过 36 小时未刷新的数据</Checkbox>
            </Space>
          </Col>
        </Row>

        {rule.updateMode === "replace" && (
          <Alert style={{ marginTop: 12 }} type="warning" showIcon
            message="替换模式会删除本地其他值，请先核对下方“删除”明细。" />
        )}
        <Row gutter={12} style={{ marginTop: 16 }}>
          <Col><Statistic title="匹配人数" value={preview?.matched ?? 0} /></Col>
          <Col><Statistic title="预计更新" value={preview?.willUpdate ?? 0} /></Col>
          <Col><Statistic title="无变化" value={preview?.unchanged ?? 0} /></Col>
          <Col><Statistic title="跳过" value={preview?.skipped ?? 0} /></Col>
        </Row>
        {skipReasonText && <div style={{ color: "#888", marginTop: 8 }}>跳过原因：{skipReasonText}</div>}
        <Space wrap style={{ marginTop: 16 }}>
          <Button onClick={() => void pullLatest()} loading={pulling}>拉取最新通讯录</Button>
          <Popconfirm
            title={`一键同步${FIELD_LABEL[field]}？`}
            description={`将先重新拉取通讯录，再按最新数据执行；预计更新 ${preview?.willUpdate ?? 0} 人。`}
            onConfirm={() => void runManual()}
          >
            <Button
              type="primary"
              loading={applying}
              disabled={!previewCurrent || loading}
            >一键同步</Button>
          </Popconfirm>
          <Button onClick={saveCurrentToAuto}>将当前条件用于自动同步</Button>
          <Button onClick={() => void loadPreview()} loading={loading}>刷新预览</Button>
        </Space>
      </Card>

      <Card title="自动同步（每天 00:00，先拉通讯录再同步）">
        {config && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Space>
              <span>自动同步总开关</span>
              <Switch checked={config.enabled}
                onChange={(enabled) => setConfig({ ...config, enabled })} />
              <Tag color={config.enabled ? "green" : "default"}>
                {config.enabled ? "已开启" : "默认关闭"}
              </Tag>
            </Space>
            <Space wrap>
              {(Object.keys(FIELD_LABEL) as EmployeeSyncField[]).map((key) => (
                <Checkbox key={key} checked={config.rules[key].enabled}
                  onChange={(event) => setConfig({
                    ...config,
                    rules: {
                      ...config.rules,
                      [key]: { ...config.rules[key], enabled: event.target.checked },
                    },
                  })}>
                  {FIELD_LABEL[key]}：{config.rules[key].enabled ? "启用" : "停用"}
                  （{SCOPE_OPTIONS.find((item) => item.value === config.rules[key].scope)?.label}
                  · {MODE_LABEL[config.rules[key].updateMode] ?? config.rules[key].updateMode}）
                </Checkbox>
              ))}
            </Space>
            <Popconfirm
              title="保存自动同步配置？"
              description={Object.values(config.rules).some((item) => item.enabled && item.updateMode === "replace")
                ? "包含高风险替换规则，请确认已检查预览。"
                : "保存后将在总开关开启时每天零点执行。"}
              onConfirm={() => void saveAutoConfig()}
            >
              <Button loading={savingConfig}>保存自动配置</Button>
            </Popconfirm>
          </Space>
        )}
      </Card>

      <Card title="同步白名单">
        <Space wrap style={{ marginBottom: 12 }}>
          <Select mode="multiple" showSearch filterOption={false}
            placeholder="搜索姓名或工号添加白名单"
            value={whitelistSelected}
            onSearch={(value) => void searchWhitelistCandidates(value)}
            onChange={setWhitelistSelected}
            options={whitelistCandidates}
            style={{ width: 440 }}
          />
          <Button onClick={() => void addWhitelist()} disabled={!whitelistSelected.length}>加入白名单</Button>
        </Space>
        <Table size="small" rowKey="empId" dataSource={whitelist}
          pagination={{ pageSize: 10 }}
          columns={[
            { title: "工号", dataIndex: "empId" },
            { title: "姓名", dataIndex: "name" },
            { title: "角色", dataIndex: "role" },
            { title: "添加人", dataIndex: "createdBy" },
            { title: "添加时间", dataIndex: "createdAt", render: (value) => new Date(value).toLocaleString() },
            {
              title: "操作",
              render: (_, row) => (
                <Popconfirm title="移出同步白名单？" onConfirm={() => void removeWhitelist(row.empId)}>
                  <Button type="link" danger>移除</Button>
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>

      <Card title="员工差异明细">
        <Space wrap style={{ marginBottom: 12 }}>
          <span>已选择 {selected.length} 人</span>
          <Popconfirm title={`同步所选员工的${FIELD_LABEL[field]}？`}
            onConfirm={() => void applySelected()}>
            <Button
              disabled={!selected.length || preview?.rule.field !== field}
              loading={applying}
            >同步所选</Button>
          </Popconfirm>
        </Space>
        <Table<EmployeeSyncPreviewItem>
          rowKey="empId"
          size="small"
          loading={loading}
          dataSource={preview?.items ?? []}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: setSelected,
            preserveSelectedRowKeys: true,
            getCheckboxProps: (row) => ({
              disabled: Boolean(row.skipReason) || !row.different,
            }),
          }}
          scroll={{ x: 1500 }}
          pagination={{
            current: page,
            pageSize,
            total: preview?.matched ?? 0,
            showSizeChanger: true,
            onChange: (next, size) => {
              setPage(size !== pageSize ? 1 : next);
              setPageSize(size);
            },
          }}
          columns={[
            { title: "工号", dataIndex: "empId", width: 120, fixed: "left" },
            { title: "姓名", dataIndex: "name", width: 120, fixed: "left" },
            { title: "钉钉职位", dataIndex: "remoteTitle", width: 140, render: (value) => value || "—" },
            { title: "部门/工作点", dataIndex: "deptPath", width: 240, render: (value) => value || "—" },
            { title: "本地值", width: 180, render: (_, row) => displayValue(row.current) },
            { title: "远程值", width: 160, render: (_, row) => displayValue(row.remote) },
            {
              title: "差异类型",
              width: 180,
              render: (_, row) => row.differenceTypes.map((value) => (
                <Tag key={value}>{DIFF_OPTIONS.find((option) => option.value === value)?.label}</Tag>
              )),
            },
            { title: "预计操作", width: 230, render: (_, row) => <PreviewChange item={row} /> },
            {
              title: "保护",
              width: 160,
              render: (_, row) => (
                <Space wrap>
                  {row.whitelisted && <Tag color="purple">白名单</Tag>}
                  {!row.remoteFresh && <Tag color="orange">数据过期</Tag>}
                  {!row.remoteActive && <Tag color="red">已离职</Tag>}
                </Space>
              ),
            },
            {
              title: "操作",
              fixed: "right",
              width: 110,
              render: (_, row) => (
                <Popconfirm title={`同步该员工的${FIELD_LABEL[field]}？`}
                  onConfirm={() => void applyOne(row.empId)}>
                  <Button type="link" disabled={
                    Boolean(row.skipReason) || !row.different || applying
                    || preview?.rule.field !== field
                    || preview?.rule.updateMode !== rule.updateMode
                  }>同步</Button>
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>
    </Space>
  );
}
