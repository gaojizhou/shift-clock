import { useEffect, useMemo, useState } from "react";
import { Button, NavBar, Selector, Toast } from "antd-mobile";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  buildScanPayload,
  getFurnaces,
  getMyChiefs,
  parseScanPayload,
  type FurnaceItem,
} from "@fdc/api-client";
import EmployeeBadge from "../components/EmployeeBadge";
import { inDingTalk, loadDD, scanQRCode } from "../dingtalk";

const ALL = "全部";

export default function ScanSimPage() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  // 补录模式：带 date 参数（非当天），选工作点后进入同款选岗向导，提交走补录
  const makeupDate = params.get("date") || "";
  const [furnaces, setFurnaces] = useState<FurnaceItem[]>([]);
  const [region, setRegion] = useState(ALL);
  const [ftype, setFtype] = useState(ALL);
  const [chief, setChief] = useState(ALL); // 全部 / 有值班长 / 无值班长
  const [myChiefs, setMyChiefs] = useState<{ userid: string; name?: string | null }[]>([]);
  const [myChief, setMyChief] = useState(ALL); // 我绑定的值班长（userid）

  useEffect(() => {
    getFurnaces().then((r) => setFurnaces(r.items));
    getMyChiefs()
      .then((r) => setMyChiefs(r.items))
      .catch(() => undefined);
  }, []);

  const regions = useMemo(() => [...new Set(furnaces.map((f) => f.region))], [furnaces]);
  const types = useMemo(() => [...new Set(furnaces.map((f) => f.furnaceType))], [furnaces]);

  const filtered = useMemo(
    () =>
      furnaces
        .filter(
          (f) =>
            (region === ALL || f.region === region) &&
            (ftype === ALL || f.furnaceType === ftype) &&
            (chief === ALL ||
              (chief === "有值班长" && !!f.chiefUserId) ||
              (chief === "无值班长" && !f.chiefUserId)) &&
            (myChief === ALL || f.chiefUserId === myChief)
        )
        .sort((a, b) =>
          a.region === b.region
            ? (a.furnaceNoOrder ?? 0) - (b.furnaceNoOrder ?? 0)
            : a.region.localeCompare(b.region)
        ),
    [furnaces, region, ftype, chief, myChief]
  );

  const goScan = (payload: string) => {
    const parsed = parseScanPayload(payload);
    if (!parsed) {
      Toast.show({ content: "无法解析工作点二维码，格式 FDC:工作点编码（如 FUR01）" });
      return;
    }
    // 进入扫码页前打标记：从扫码页返回本页时不再自动唤起相机，直接显示选择页
    sessionStorage.setItem("fdc_scan_returned", "1");
    // 把原始二维码交给扫码页按工作点 code 统一解析；补录模式透传 date
    const dateQs = makeupDate ? `&date=${encodeURIComponent(makeupDate)}` : "";
    nav(`/scan?scan=${encodeURIComponent(payload)}${dateQs}`, { state: { scanPayload: payload } });
  };

  // 钉钉在线且非补录：本页是「强制扫码」页——取消/失败不回选择页，直接回首页
  const forceScan = inDingTalk() && !makeupDate;
  const [scanning, setScanning] = useState(false);
  const dtScan = async () => {
    if (scanning) return;
    setScanning(true);
    try {
      const dd = await loadDD();
      const text = await scanQRCode(dd);
      if (text) {
        goScan(text);
        return;
      }
      if (forceScan) {
        nav("/", { replace: true });
        return;
      }
      Toast.show({ content: "未识别到二维码内容" });
    } catch {
      if (forceScan) {
        nav("/", { replace: true });
        return;
      }
      Toast.show({ content: "唤起相机扫码失败，请手动选择" });
    } finally {
      setScanning(false);
    }
  };

  // 钉钉在线：进入即强制唤起相机；从扫码页返回则直接回首页（不再停留在选择页）。
  // 非钉钉/补录：保持显示选择页。
  useEffect(() => {
    if (!forceScan) return;
    if (sessionStorage.getItem("fdc_scan_returned")) {
      sessionStorage.removeItem("fdc_scan_returned");
      nav("/", { replace: true });
      return;
    }
    void dtScan();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filterRow = (label: string, value: string, setValue: (v: string) => void, opts: string[]) => (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 12, color: "#999", marginBottom: 4 }}>{label}</div>
      <Selector
        options={[ALL, ...opts].map((o) => ({ label: o, value: o }))}
        value={[value]}
        onChange={(v) => v[0] && setValue(v[0] as string)}
        style={{ "--gap": "6px" } as React.CSSProperties}
      />
    </div>
  );

  return (
    <div style={{ background: "#f5f5f5", minHeight: "100vh" }}>
      <NavBar
        onBack={() => nav(makeupDate ? "/calendar" : "/")}
        style={{ background: makeupDate ? "#fa8c16" : "#1677FF", color: "#fff" }}
      >
        {makeupDate ? "补录 · 选择工作点" : "选择用工类型"}
      </NavBar>
      {makeupDate && (
        <div
          data-testid="makeup-banner"
          style={{
            margin: "12px 12px 0",
            padding: "10px 12px",
            background: "#fff7e6",
            border: "1px solid #ffd591",
            borderRadius: 8,
            fontSize: 13,
            color: "#ad6800",
          }}
        >
          正在为 <strong>{makeupDate}</strong> 补录：扫工作点码或在下方手动选择工作点。
        </div>
      )}
      <div style={{ margin: 12, padding: 12, background: "#fff", borderRadius: 8 }}>
        <EmployeeBadge />
        <Button
          block
          color="primary"
          loading={scanning}
          data-testid="dt-scan-furnace"
          style={{ marginTop: 12 }}
          onClick={() => void dtScan()}
        >
          {makeupDate ? "钉钉扫一扫工作点码（补录）" : "钉钉扫一扫上岗"}
        </Button>
      </div>
      <div style={{ margin: 12, padding: 12, background: "#fff", borderRadius: 8 }}>
        <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>选择基地与工作点</div>
        {filterRow("地区", region, setRegion, regions)}
        {filterRow("设备类型", ftype, setFtype, types)}
        {filterRow("值班长", chief, setChief, ["有值班长", "无值班长"])}
        {myChiefs.length > 0 && (
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 12, color: "#999", marginBottom: 4 }}>我的值班长</div>
            <Selector
              options={[
                { label: ALL, value: ALL },
                ...myChiefs.map((c) => ({ label: `${c.name ?? c.userid}值班长`, value: c.userid })),
              ]}
              value={[myChief]}
              onChange={(v) => v[0] && setMyChief(v[0] as string)}
              style={{ "--gap": "6px" } as React.CSSProperties}
            />
          </div>
        )}
      </div>

      <div style={{ margin: "0 12px 12px" }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
          {filtered.map((f) => (
            <button
              key={`${f.region}-${f.furnaceNo}`}
              type="button"
              data-testid={`pick-furnace-${f.furnaceNo}`}
              onClick={() => goScan(buildScanPayload(f.code ?? ""))}
              style={{
                padding: 10,
                textAlign: "left",
                border: "1px solid #e8e8e8",
                borderRadius: 8,
                background: "#fff",
                minHeight: 76,
              }}
            >
              <div style={{ fontWeight: 600, fontSize: 15 }}>{f.furnaceNo}</div>
              <div style={{ fontSize: 11, color: "#aaa", marginTop: 2 }}>{f.furnaceType}</div>
              <div style={{ fontSize: 11, marginTop: 4, color: f.chiefName ? "#1677FF" : "#ccc" }}>
                {f.chiefName ? `值班长：${f.chiefName}` : "未绑定值班长"}
              </div>
            </button>
          ))}
        </div>
        {filtered.length === 0 && (
          <div style={{ textAlign: "center", color: "#bbb", padding: 24 }}>无匹配工作点</div>
        )}
      </div>
    </div>
  );
}
