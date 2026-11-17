import { useEffect, useRef, useState } from "react";
import { Button, Input, NavBar, TextArea, Toast } from "antd-mobile";
import { useNavigate } from "react-router-dom";
import { apiGet, apiPost } from "@fdc/api-client";
import { corpIdFromUrl, inDingTalk, loadDD, requestAuthCode, scanQRCode } from "../dingtalk";

export default function DebugPage() {
  const nav = useNavigate();
  const [logs, setLogs] = useState<string[]>([]);
  const [result, setResult] = useState<unknown>(null);
  const [running, setRunning] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [scanText, setScanText] = useState<string | null>(null);
  const started = useRef(false);

  const log = (msg: string) =>
    setLogs((prev) => [...prev, `${new Date().toLocaleTimeString()}  ${msg}`]);

  const scan = async () => {
    if (scanning) return;
    setScanning(true);
    setScanText(null);
    try {
      if (!inDingTalk()) {
        log("非钉钉环境：无法唤起相机扫码。请在钉钉中打开。");
        Toast.show({ content: "请在钉钉中打开" });
        return;
      }
      log("加载钉钉 JSAPI ...");
      const dd = await loadDD();
      log("唤起相机扫码 ...");
      const text = await scanQRCode(dd);
      setScanText(text);
      log(`扫码内容: ${text || "(空)"} ✓`);
      Toast.show({ content: "扫码成功" });
    } catch (e: any) {
      const msg = e?.message ?? (typeof e === "string" ? e : JSON.stringify(e));
      log(`✗ 扫码失败: ${msg}`);
      Toast.show({ content: `扫码失败: ${msg}` });
    } finally {
      setScanning(false);
    }
  };

  const [pushing, setPushing] = useState(false);
  // 推送目标钉钉ID：默认当前用户（免登拿到后回填）；消息内容可自定义
  const [pushDd, setPushDd] = useState(localStorage.getItem("fdc_userid") || "");
  const [pushText, setPushText] = useState(
    "【三班倒打卡】测试推送：您有待确认的当日出勤名单，请进入值班长日确认查看。"
  );
  const pushTest = async () => {
    if (pushing) return;
    setPushing(true);
    try {
      log(`推送测试 → 钉钉ID: ${pushDd || "(当前登录人)"}`);
      const res = await apiPost<{
        ok?: boolean;
        to?: string;
        error?: string;
        forbidden?: { userid: string; code: string; reason: string }[];
        invalidUsers?: string[];
        logs?: string[];
        result?: { task_id?: number };
        sendResult?: unknown;
      }>("/dingtalk/push-test", {
        ddUserId: pushDd.trim() || undefined,
        text: pushText.trim() || undefined,
      });
      (res.logs ?? []).forEach((l) => log(l));
      if (res.ok === false) {
        // 钉钉受理成功但实际未送达（不在可见范围/被禁止）→ 按错误展示
        log(`✗ 推送失败：${res.error ?? "部分用户未送达"}`);
        (res.forbidden ?? []).forEach((f) =>
          log(`  · ${f.userid} 被禁止接收（${f.code || "无码"}：${f.reason}）`)
        );
        if (res.sendResult) log(`投递结果: ${JSON.stringify(res.sendResult)}`);
        Toast.show({ content: `推送失败：${res.error ?? "部分用户未送达"}` });
      } else {
        log(`推送受理 ✓ to=${res.to ?? "-"} task_id=${res.result?.task_id ?? "-"}`);
        if (res.sendResult) log(`投递结果: ${JSON.stringify(res.sendResult)}`);
        Toast.show({ content: "已推送，投递正常" });
      }
    } catch (e: any) {
      const msg = e?.data?.detail ?? e?.message ?? (typeof e === "string" ? e : JSON.stringify(e));
      log(`✗ 推送失败: ${msg}`);
      Toast.show({ content: `推送失败: ${msg}` });
    } finally {
      setPushing(false);
    }
  };

  const run = async () => {
    if (running) return;
    setRunning(true);
    setResult(null);
    setLogs([]);
    try {
      const isDD = inDingTalk();
      log(`UA: ${navigator.userAgent}`);
      log(`页面: ${location.href}`);
      log(`在钉钉客户端内: ${isDD ? "是" : "否"}`);

      let cfg: any = {};
      try {
        cfg = await apiGet<{ corpId: string; agentId: string; appKey: string; configured: boolean }>(
          "/dingtalk/config"
        );
        log(`后端配置: corpId=${cfg.corpId || "(空)"} agentId=${cfg.agentId || "(空)"} 服务端凭据=${cfg.configured ? "已配置" : "未配置"}`);
        log(`服务端换码应用 appKey=${cfg.appKey || "(空)"}（须与打开本H5的应用一致，否则换码报 40078）`);
      } catch (e: any) {
        log(`读取 /dingtalk/config 失败: ${e?.message ?? e}`);
      }
      // 自检：服务端 gettoken 是否成功（验证凭据有效性，区分「凭据无效」vs「应用不匹配」）
      try {
        const diag = await apiGet<Record<string, unknown>>("/dingtalk/diag");
        log(`换码应用 gettoken: ${diag.tokenOk ? "成功 ✓(凭据有效)" : "失败 ✗"}${diag.tokenError ? " — " + diag.tokenError : ""}`);
      } catch (e: any) {
        log(`读取 /dingtalk/diag 失败: ${e?.message ?? e}`);
      }

      if (!isDD) {
        log("非钉钉环境：无法调用 JSAPI 免登。请在钉钉中打开本页面。");
        return;
      }

      log("加载钉钉 JSAPI ...");
      const dd = await loadDD();
      log("JSAPI ready ✓");

      const corpId = cfg.corpId || corpIdFromUrl();
      log(`使用 corpId: ${corpId || "(空 — 内部应用通常仍可免登)"}`);

      log("请求免登 authCode ...");
      const code = await requestAuthCode(dd, corpId);
      log(`authCode: ${code} ✓`);

      log("用 authCode 换取用户信息 ...");
      const info = await apiPost<Record<string, unknown>>("/dingtalk/login", { authCode: code });
      setResult(info);
      // 免登拿到真实钉钉 userid 后回填推送目标
      if ((info as any).userid) setPushDd(String((info as any).userid));
      if ((info as any).configured === false) {
        log("服务端凭据未配置：仅回显 authCode（免登链路已打通 ✓）");
      } else {
        log(`用户信息获取成功 ✓  userid=${(info as any).userid}  name=${(info as any).name ?? "-"}`);
      }
    } catch (e: any) {
      // 优先展示后端返回的真实错误（如 502「dingtalk error: getuserinfo failed: {errcode...}」）
      const detail = e?.data?.detail ?? e?.data?.error;
      const msg = detail ?? e?.message ?? (typeof e === "string" ? e : JSON.stringify(e));
      log(`✗ 出错: ${msg}`);
      if (e?.status) log(`  HTTP ${e.status}`);
      if (e?.data && Object.keys(e.data).length) log(`  detail: ${JSON.stringify(e.data)}`);
      Toast.show({ content: `失败: ${msg}` });
    } finally {
      setRunning(false);
    }
  };

  // 在钉钉内自动跑一次
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (inDingTalk()) void run();
    else log("提示：当前不在钉钉客户端内，点击下方按钮可查看环境检测结果。");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div style={{ paddingBottom: 24 }}>
      <NavBar onBack={() => nav("/")}>钉钉免登调试</NavBar>

      <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
        <Button
          color="primary"
          block
          loading={running}
          data-testid="dt-debug-run"
          onClick={() => void run()}
        >
          {inDingTalk() ? "重新获取用户信息" : "运行环境检测"}
        </Button>
        <Button
          color="success"
          block
          loading={scanning}
          data-testid="dt-debug-scan"
          onClick={() => void scan()}
        >
          唤起钉钉相机扫码
        </Button>
        <div
          style={{
            padding: 12,
            background: "#fff",
            borderRadius: 8,
            border: "1px solid #eee",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          <div style={{ fontWeight: 600 }}>推送测试（工作通知）</div>
          <div style={{ fontSize: 12, color: "#999" }}>目标钉钉ID（默认当前用户，可改）</div>
          <Input
            data-testid="dt-push-dd"
            value={pushDd}
            onChange={setPushDd}
            placeholder="钉钉 userid"
            style={{ border: "1px solid #ddd", borderRadius: 8, padding: "6px 10px" }}
          />
          <div style={{ fontSize: 12, color: "#999" }}>消息内容</div>
          <TextArea
            data-testid="dt-push-text"
            value={pushText}
            onChange={setPushText}
            rows={3}
            style={{ border: "1px solid #ddd", borderRadius: 8, padding: "6px 10px" }}
          />
          <Button
            color="warning"
            block
            loading={pushing}
            data-testid="dt-debug-push"
            onClick={() => void pushTest()}
          >
            发送测试推送
          </Button>
        </div>
      </div>

      {scanText != null && (
        <div style={{ padding: "0 12px 12px" }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>扫码内容</div>
          <pre
            data-testid="dt-debug-scan-result"
            style={{
              background: "#e6f4ff",
              border: "1px solid #91caff",
              borderRadius: 8,
              padding: 12,
              fontSize: 13,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              margin: 0,
            }}
          >
            {scanText || "(空)"}
          </pre>
        </div>
      )}

      {result != null && (
        <div style={{ padding: "0 12px 12px" }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>返回结果</div>
          <pre
            data-testid="dt-debug-result"
            style={{
              background: "#f6ffed",
              border: "1px solid #b7eb8f",
              borderRadius: 8,
              padding: 12,
              fontSize: 12,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              margin: 0,
            }}
          >
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}

      <div style={{ padding: "0 12px" }}>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>执行日志</div>
        <div
          style={{
            background: "#fafafa",
            border: "1px solid #eee",
            borderRadius: 8,
            padding: 12,
            fontSize: 12,
            fontFamily: "monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-all",
            minHeight: 120,
          }}
        >
          {logs.length === 0 ? "（暂无日志）" : logs.map((l, i) => <div key={i}>{l}</div>)}
        </div>
      </div>
    </div>
  );
}
