import { useCallback, useEffect, useState } from "react";
import { Button, Card, NavBar, Tag, Toast, Dialog, Empty, Popup, List } from "antd-mobile";
import { useNavigate } from "react-router-dom";
import { apiGet, apiPost, getMyNotifications, isHalfShift, workHoursLabel, type MyNotification } from "@fdc/api-client";

// 通知时间戳（后端 ISO，UTC）→ 本地「MM-DD HH:mm」
function fmtNoteTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

type MakeupItem = {
  empId: string; name: string; date: string; region: string; furnace: string;
  squad: string | null; post: string; employType: string; reason: string | null;
};
type DelegatedItem = {
  empId: string; name: string; date: string; furnace: string; status: string; dataSource: string;
};
type Queue = { date: string; makeups: MakeupItem[]; delegated: DelegatedItem[]; count: number };

type Evidence = {
  empId: string; date: string; region: string; furnace: string; status: string;
  attendanceResultWorkHours?: string | null;
  laoqinPunches: { time: string; inoutType: string; source: string | null; makeupReason?: string | null }[];
  scanRecords: {
    time: string; region: string; furnace: string; post: string; squad: string | null;
    employType: string; source: string; workHours?: number | null;
  }[];
  makeups: {
    region: string; furnace: string; squad: string | null; post: string;
    employType: string; kind: string; status: string; reason: string | null;
  }[];
  overtimes?: {
    id: number; naturalDate: string; region: string; furnace: string; squad: string | null; post: string;
    employType: string; workHours: number; approver?: string | null;
  }[];
};

type Detail =
  | { kind: "makeup"; item: MakeupItem }
  | { kind: "delegated"; item: DelegatedItem };

const fmt = (iso: string) => iso.slice(0, 19).replace("T", " ");
const fmtHours = (v: string | null | undefined) => {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? `${Number(n.toFixed(2))}小时` : `${v}小时`;
};

export default function ChiefReviewPage() {
  const nav = useNavigate();
  // 审核日期默认不选 → 看所有日期的待办（数据多，下面按段翻页）
  const [date, setDate] = useState("");
  const [data, setData] = useState<Queue | null>(null);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState<MyNotification[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [evLoading, setEvLoading] = useState(false);
  const PAGE = 10;
  const [mPage, setMPage] = useState(1);
  const [dPage, setDPage] = useState(1);
  const [nPage, setNPage] = useState(1);

  useEffect(() => {
    getMyNotifications(100)
      .then((r) => { setNotes(r.items); setNPage(1); })
      .catch(() => setNotes([]));
  }, []);

  const load = useCallback(() => {
    apiGet<Queue>(`/chief/review${date ? `?date=${date}` : ""}`).then(setData).catch(() => setData(null));
  }, [date]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { setMPage(1); setDPage(1); }, [data]);

  const openDetail = (d: Detail) => {
    setDetail(d);
    setEvidence(null);
    setEvLoading(true);
    apiGet<Evidence>(`/anomalies/evidence?empId=${encodeURIComponent(d.item.empId)}&date=${d.item.date}`)
      .then(setEvidence)
      .catch(() => Toast.show({ content: "加载详情失败" }))
      .finally(() => setEvLoading(false));
  };

  const doReviewMakeup = async (m: MakeupItem, action: "approve" | "reject") => {
    setBusy(true);
    try {
      await apiPost("/makeup/review", { userid: m.empId, date: m.date, furnace: m.furnace, region: m.region, action });
      Toast.show({ content: action === "approve" ? "已通过，按扫码参与对账" : "已拒绝" });
      setDetail(null);
      load();
    } catch {
      Toast.show({ content: "操作失败" });
    } finally { setBusy(false); }
  };

  const doConfirmDelegated = async (d: DelegatedItem, status: "通过" | "不通过") => {
    setBusy(true);
    try {
      await apiPost("/anomalies/confirm", { empId: d.empId, date: d.date, kind: "chief", status });
      Toast.show({ content: `已${status}` });
      setDetail(null);
      load();
    } catch {
      Toast.show({ content: "操作失败" });
    } finally { setBusy(false); }
  };

  // 二次确认后再执行
  const askMakeup = (m: MakeupItem, action: "approve" | "reject") => {
    void Dialog.confirm({
      content: `确认${action === "approve" ? "通过" : "拒绝"} ${m.name} 的补录？`,
      confirmText: action === "approve" ? "确认通过" : "确认拒绝",
      onConfirm: () => doReviewMakeup(m, action),
    });
  };
  const askDelegated = (d: DelegatedItem, status: "通过" | "不通过") => {
    void Dialog.confirm({
      content: `确认将 ${d.name} 的班次设为「${status}」？`,
      confirmText: `确认${status}`,
      onConfirm: () => doConfirmDelegated(d, status),
    });
  };

  const Pager = ({ page, total, onPage }: { page: number; total: number; onPage: (p: number) => void }) => {
    const pages = Math.max(1, Math.ceil(total / PAGE));
    if (pages <= 1) return null;
    return (
      <div style={{ display: "flex", gap: 10, alignItems: "center", justifyContent: "center", margin: "6px 0" }}>
        <Button size="mini" disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</Button>
        <span style={{ fontSize: 12, color: "#888" }}>第 {page}/{pages} 页</span>
        <Button size="mini" disabled={page >= pages} onClick={() => onPage(page + 1)}>下一页</Button>
      </div>
    );
  };

  return (
    <div style={{ background: "#f5f5f5", minHeight: "100vh" }}>
      <NavBar onBack={() => nav("/")} style={{ background: "#1677FF", color: "#fff" }}>
        值班长审核确认
      </NavBar>
      <div style={{ padding: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <span style={{ fontSize: 12, color: "#999" }}>审核日期</span>
          <input
            type="date"
            value={date}
            data-testid="review-date"
            onChange={(e) => setDate(e.target.value)}
            style={{ fontSize: 13, padding: "4px 8px", border: "1px solid #ddd", borderRadius: 6 }}
          />
          {date && <Button size="mini" fill="none" style={{ fontSize: 12 }} onClick={() => setDate("")}>全部</Button>}
          <span style={{ fontSize: 12, color: "#999" }}>共 {data?.count ?? 0} 项待处理（留空=全部日期）</span>
        </div>

        {notes.length > 0 && (
          <Card style={{ marginBottom: 8 }} data-testid="my-notifications">
            <div style={{ fontWeight: 600, marginBottom: 6 }}>📣 我的通知（{notes.length}）</div>
            {notes.slice((nPage - 1) * PAGE, nPage * PAGE).map((n) => (
              <div key={n.id} style={{ fontSize: 12, color: "#666", padding: "4px 0", borderBottom: "1px dashed #f0f0f0" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8 }}>
                  <span style={{ color: n.kind === "staffing_shortage" ? "#fa8c16" : "#1677FF" }}>
                    {n.subject || n.kind || "通知"}
                  </span>
                  {n.ts ? (
                    <span style={{ color: "#bbb", fontSize: 11, flexShrink: 0 }}>{fmtNoteTime(n.ts)}</span>
                  ) : null}
                </div>
                {n.detail ? <div style={{ marginTop: 2 }}>{n.detail}</div> : null}
                {n.pushNote ? (
                  <div style={{ marginTop: 2, color: "#cf1322" }}>⚠ {n.pushNote}</div>
                ) : null}
              </div>
            ))}
            <Pager page={nPage} total={notes.length} onPage={setNPage} />
          </Card>
        )}

        <div style={{ fontWeight: 600, margin: "8px 0" }}>
          补录待审
        </div>
        {data?.makeups.length ? (
          <>
            {data.makeups.slice((mPage - 1) * PAGE, mPage * PAGE).map((m) => (
              <Card key={`m-${m.empId}-${m.date}`} style={{ marginBottom: 8 }} data-testid={`makeup-${m.empId}`}>
                <div style={{ fontWeight: 600 }}>{m.name}<span style={{ color: "#bbb", marginLeft: 6, fontSize: "0.9rem" }}>{m.empId}</span></div>
                <div style={{ fontSize: "0.98rem", color: "#666", margin: "4px 0" }}>
                  {m.date} · {m.region} {m.furnace} · {m.squad ?? "不分班"} · {m.post} · {m.employType}
                </div>
                {m.reason && <div style={{ fontSize: "0.9rem", color: "#999" }}>原因：{m.reason}</div>}
                <div style={{ marginTop: 10 }}>
                  <Button size="small" data-testid={`makeup-detail-${m.empId}`} onClick={() => openDetail({ kind: "makeup", item: m })}>
                    查看详情
                  </Button>
                </div>
              </Card>
            ))}
            <Pager page={mPage} total={data.makeups.length} onPage={setMPage} />
          </>
        ) : <Empty description="无待审补录" style={{ padding: 12 }} />}

        <div style={{ fontWeight: 600, margin: "16px 0 8px" }}>
          转交给值班长确认
        </div>
        {data?.delegated.length ? (
          <>
            {data.delegated.slice((dPage - 1) * PAGE, dPage * PAGE).map((d) => (
              <Card key={`d-${d.empId}-${d.date}`} style={{ marginBottom: 8 }} data-testid={`delegated-${d.empId}`}>
                <div style={{ fontWeight: 600 }}>{d.name}<span style={{ color: "#bbb", marginLeft: 6, fontSize: "0.9rem" }}>{d.empId}</span></div>
                <div style={{ fontSize: "0.98rem", color: "#666", margin: "4px 0" }}>
                  {d.date} · {d.furnace} · 来源{d.dataSource} · 对账 <Tag color="warning">{d.status}</Tag>
                </div>
                <div style={{ marginTop: 10 }}>
                  <Button size="small" data-testid={`delegated-detail-${d.empId}`} onClick={() => openDetail({ kind: "delegated", item: d })}>
                    查看详情
                  </Button>
                </div>
              </Card>
            ))}
            <Pager page={dPage} total={data.delegated.length} onPage={setDPage} />
          </>
        ) : <Empty description="无 HR 转交项" style={{ padding: 12 }} />}
      </div>

      {/* 详情弹窗：证据链信息 + 二次确认操作 */}
      <Popup
        visible={!!detail}
        onMaskClick={() => setDetail(null)}
        showCloseButton
        onClose={() => setDetail(null)}
        bodyStyle={{ maxHeight: "82vh", overflow: "auto", borderTopLeftRadius: 12, borderTopRightRadius: 12 }}
      >
        {detail && (
          <div style={{ padding: 16, fontSize: "1rem", lineHeight: 1.55 }} data-testid="chief-review-detail">
            <div style={{ fontSize: "1.18rem", fontWeight: 700, marginBottom: 4 }}>
              {detail.kind === "makeup" ? "补录详情" : "HR 转交详情"}
            </div>
            <div style={{ fontSize: "1rem", color: "#666", marginBottom: 10 }}>
              {detail.item.name}（{detail.item.empId}） · {detail.item.date}
              {detail.kind === "delegated" && (
                <>
                  <br />对账：<Tag color="warning">{detail.item.status}</Tag> 来源：{detail.item.dataSource}
                </>
              )}
              {detail.kind === "makeup" && (
                <>
                  <br />{detail.item.region} {detail.item.furnace} · {detail.item.squad ?? "不分班"} · {detail.item.post} · {detail.item.employType}
                  {detail.item.reason ? ` · 原因：${detail.item.reason}` : ""}
                </>
              )}
            </div>

            {evLoading ? (
              <div style={{ color: "#888", padding: 8 }}>加载中…</div>
            ) : evidence ? (
              <>
                <div style={{ fontSize: "0.98rem", color: "#999", margin: "8px 0 2px" }}>
                  地区/工作点：{evidence.region} · {evidence.furnace} ｜ 对账：{evidence.status} ｜ 出勤工时：{fmtHours(evidence.attendanceResultWorkHours)}
                </div>
                <div style={{ fontWeight: 600, marginTop: 8 }}>工作点扫码</div>
                {evidence.scanRecords.length ? (
                  <List>
                    {evidence.scanRecords.map((s, i) => (
                      <List.Item key={`s${i}`}>
                        <span style={{ fontSize: "1rem" }}>
                          {fmt(s.time)} · {s.region}{s.furnace} · {s.squad ?? "不分班"} · {s.post} · {s.employType}
                          {isHalfShift(s.workHours) ? ` · ${workHoursLabel(s.workHours)}` : ""} · 来源{s.source}
                        </span>
                      </List.Item>
                    ))}
                  </List>
                ) : <div style={{ color: "#bbb", fontSize: "1rem", padding: "2px 0" }}>（无）</div>}

                <div style={{ fontWeight: 600, marginTop: 8 }}>补录 / 代签单</div>
                {evidence.makeups.length ? (
                  <List>
                    {evidence.makeups.map((m, i) => (
                      <List.Item key={`mk${i}`}>
                        <span style={{ fontSize: "1rem" }}>
                          {m.kind} · {m.status} · {m.region}{m.furnace} · {m.squad ?? "不分班"} · {m.post} · {m.employType}
                          {m.reason ? ` · ${m.reason}` : ""}
                        </span>
                      </List.Item>
                    ))}
                  </List>
                ) : <div style={{ color: "#bbb", fontSize: "1rem", padding: "2px 0" }}>（无）</div>}

                <div style={{ fontWeight: 600, marginTop: 8 }}>加班</div>
                {(evidence.overtimes ?? []).length ? (
                  <List>
                    {(evidence.overtimes ?? []).map((o) => (
                      <List.Item key={`ot${o.id}`}>
                        <span style={{ fontSize: "1rem" }}>
                          OT#{o.id} · {o.naturalDate} · {o.region}{o.furnace} · {o.squad ?? "不分班"} · {o.post} · {o.employType} · {o.workHours}h
                          {o.approver ? ` · 经办${o.approver}` : ""}
                        </span>
                      </List.Item>
                    ))}
                  </List>
                ) : <div style={{ color: "#bbb", fontSize: "1rem", padding: "2px 0" }}>（无）</div>}

                <div style={{ fontWeight: 600, marginTop: 8 }}>钉钉打卡</div>
                {evidence.laoqinPunches.length ? (
                  <List>
                    {evidence.laoqinPunches.map((p, i) => (
                      <List.Item key={`p${i}`}>
                        <span style={{ fontSize: "1rem" }}>{fmt(p.time)} · {p.inoutType}{p.source ? ` · ${p.source}` : ""}{p.makeupReason ? ` · ${p.makeupReason}` : ""}</span>
                      </List.Item>
                    ))}
                  </List>
                ) : <div style={{ color: "#bbb", fontSize: "1rem", padding: "2px 0" }}>（无）</div>}
              </>
            ) : (
              <div style={{ color: "#888", padding: 8 }}>暂无详情数据</div>
            )}

            {/* 操作（二次确认） */}
            <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
              {detail.kind === "makeup" ? (
                <>
                  <Button block color="primary" loading={busy} data-testid="detail-makeup-approve"
                    onClick={() => askMakeup(detail.item, "approve")}>通过</Button>
                  <Button block color="danger" fill="outline" loading={busy} data-testid="detail-makeup-reject"
                    onClick={() => askMakeup(detail.item, "reject")}>拒绝</Button>
                </>
              ) : (
                <>
                  <Button block color="primary" loading={busy} data-testid="detail-delegated-pass"
                    onClick={() => askDelegated(detail.item, "通过")}>通过</Button>
                  <Button block color="danger" fill="outline" loading={busy} data-testid="detail-delegated-reject"
                    onClick={() => askDelegated(detail.item, "不通过")}>不通过</Button>
                </>
              )}
            </div>
          </div>
        )}
      </Popup>
    </div>
  );
}
