import { useEffect, useMemo, useState } from "react";
import { Button, NavBar, Popup, SpinLoading } from "antd-mobile";
import { useNavigate } from "react-router-dom";
import { apiGet, getDemoContext, isHalfShift } from "@fdc/api-client";
import { useAuth } from "../auth";

type DayDetail = {
  furnace?: string;
  region?: string;
  furnaceType?: string | null;
  chief?: string | null;
  post?: string;
  squad?: string | null;
  employType?: string;
  workHours?: number;
  scanTime?: string | null;
  source?: string | null;
  confirmStatus?: string | null;
  overtimes?: {
    furnace?: string;
    region?: string;
    post: string;
    squad: string | null;
    employType: string;
    workHours: number;
    reconDingtalk: boolean;
  }[];
};

type DayItem = {
  day: number;
  date: string;
  status: string;
  canMakeup: boolean;
  detail?: DayDetail | null;
};

type Attendance = {
  userid: string;
  name?: string;
  month: string;
  days: DayItem[];
};

const STATUS_COLOR: Record<string, string> = {
  正常: "#52c41a",
  未到: "#ff4d4f",
  待审: "#faad14",
  异常: "#fa8c16",
  已确认: "#1677ff",
  无数据: "#bfbfbf",
};

// 数据来源配色：扫码 / 补录 / 代签
const SOURCE_COLOR: Record<string, string> = {
  扫码: "#1677ff",
  补录: "#faad14",
  代签: "#722ed1",
};

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

/** 周一开始的星期偏移：JS getDay() 0=周日…6=周六 → 周一=0…周日=6 */
function mondayOffset(year: number, monthIndex0: number): number {
  const wd = new Date(year, monthIndex0, 1).getDay();
  return (wd + 6) % 7;
}

export default function CalendarPage() {
  const nav = useNavigate();
  const { me } = useAuth();
  const [month, setMonth] = useState("2026-04");
  const [today, setToday] = useState(""); // 真实“今天”(YYYY-MM-DD)，由 demo context 提供
  const userid = me.userid || localStorage.getItem("fdc_userid") || "T0003";
  const name = me.name || "";
  const [data, setData] = useState<Attendance | null>(null);
  const [loading, setLoading] = useState(true);
  const [popup, setPopup] = useState(false);
  const [selected, setSelected] = useState<DayItem | null>(null);

  useEffect(() => {
    getDemoContext().then((c) => {
      setMonth(c.month);
      setToday(c.today);
    });
  }, []);

  useEffect(() => {
    if (me.userid) localStorage.setItem("fdc_userid", me.userid);
  }, [me.userid]);

  useEffect(() => {
    // 取消感知：月份/用工号切换时让旧请求响应作废。否则初始月(2026-04)的请求若比新月晚返回，
    // 会用旧月数据覆盖 data，导致头部(用 month)显示新月、网格(用 data.days)却是旧月 → 点击日期错月。
    let ignore = false;
    setLoading(true);
    apiGet<Attendance>(
      `/attendance?userid=${encodeURIComponent(userid)}&month=${month}`
    )
      .then((d) => {
        if (!ignore) setData(d);
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [userid, month]);

  const [year, mon] = month.split("-").map(Number);
  const offset = useMemo(() => mondayOffset(year, mon - 1), [year, mon]);

  // 是否当前(真实今天所在)月——决定“下一月”能否点
  const isCurrentMonth = today.slice(0, 7) === month;

  const shiftMonth = (delta: number) => {
    if (delta > 0 && isCurrentMonth) return; // 不允许翻到未来整月
    const d = new Date(year, mon - 1 + delta, 1);
    const mm = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    setMonth(mm);
  };

  const openDay = (d: DayItem, future: boolean) => {
    if (future) return; // 未来日期不可操作
    setSelected(d);
    setPopup(true);
  };

  // 补录走与扫码一致的完整流程：选工作点 → 班次/用工/半班/工种 → 提交待审
  const goMakeup = () => {
    if (!selected) return;
    setPopup(false);
    nav(`/scan-sim?date=${encodeURIComponent(selected.date)}`);
  };

  const detail = selected?.detail;
  // 仅用与当前显示月份一致的数据，避免请求在途/乱序时网格沿用旧月日期（与头部月份错位）
  const days = data?.month === month ? (data?.days ?? []) : [];
  const cells: (DayItem | null)[] = [...Array(offset).fill(null), ...days];

  const fmtTime = (iso?: string | null) => (iso ? iso.replace("T", " ").slice(0, 16) : "—");

  return (
    <div style={{ background: "#f5f5f5", minHeight: "100vh", paddingBottom: 24 }}>
      <NavBar onBack={() => nav(-1)} style={{ background: "#1677FF", color: "#fff" }}>
        工作点打卡记录
      </NavBar>

      {/* 月份大标题 + 翻月 */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "center",
          gap: 16,
          padding: "20px 12px 8px",
        }}
      >
        <span
          data-testid="cal-prev"
          onClick={() => shiftMonth(-1)}
          style={{ fontSize: 24, color: "#1677ff", cursor: "pointer", userSelect: "none" }}
        >
          ‹
        </span>
        <div style={{ textAlign: "center", minWidth: 150 }}>
          <div style={{ fontSize: 15, color: "#999", letterSpacing: 2 }}>{year} 年</div>
          <div style={{ fontSize: 40, fontWeight: 800, lineHeight: 1.1, color: "#222" }}>
            {mon} 月
          </div>
        </div>
        <span
          data-testid="cal-next"
          onClick={() => shiftMonth(1)}
          style={{
            fontSize: 24,
            color: isCurrentMonth ? "#d9d9d9" : "#1677ff",
            cursor: isCurrentMonth ? "not-allowed" : "pointer",
            userSelect: "none",
          }}
        >
          ›
        </span>
      </div>
      <div style={{ textAlign: "center", fontSize: 12, color: "#999", marginBottom: 8 }}>
        {name || userid}
      </div>

      {/* 星期表头（周一→周日） */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(7, 1fr)",
          padding: "0 8px",
          marginBottom: 4,
        }}
      >
        {WEEKDAYS.map((w, i) => (
          <div
            key={w}
            style={{
              textAlign: "center",
              fontSize: 12,
              fontWeight: 600,
              color: i >= 5 ? "#ff7875" : "#999",
              paddingBottom: 4,
            }}
          >
            {w}
          </div>
        ))}
      </div>

      {/* 日期网格 */}
      {loading && data?.month !== month ? (
        <div
          data-testid="cal-loading"
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 10,
            padding: "56px 0",
            color: "#999",
          }}
        >
          <SpinLoading style={{ "--size": "32px" }} />
          <span style={{ fontSize: 13 }}>加载打卡记录…</span>
        </div>
      ) : (
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(7, 1fr)",
          gap: 4,
          padding: "0 8px",
        }}
      >
        {cells.map((d, idx) => {
          if (!d) return <div key={`blank-${idx}`} />;
          const isToday = d.date === today;
          const future = !!today && d.date > today;
          const dt = d.detail;
          const hasData = !!dt && !!dt.furnace;
          const statusColor = STATUS_COLOR[d.status] ?? "#bfbfbf";
          const srcColor = (dt?.source && SOURCE_COLOR[dt.source]) || "#999";
          return (
            <div
              key={d.date}
              data-testid={`cal-day-${d.day}`}
              onClick={() => openDay(d, future)}
              style={{
                minHeight: 70,
                padding: 4,
                borderRadius: 8,
                border: isToday ? "2px solid #1677ff" : "1px solid #eee",
                background: future ? "#fafafa" : "#fff",
                opacity: future ? 0.45 : 1,
                cursor: future ? "default" : "pointer",
                display: "flex",
                flexDirection: "column",
                overflow: "hidden",
              }}
            >
              {/* 日期数字 + 今天标记 */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <span
                  style={{
                    fontSize: 13,
                    fontWeight: isToday ? 800 : 600,
                    color: isToday ? "#1677ff" : "#333",
                  }}
                >
                  {d.day}
                </span>
                {isToday && (
                  <span style={{ fontSize: 9, color: "#1677ff", fontWeight: 700 }}>今</span>
                )}
                {!isToday && hasData && (
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      background: statusColor,
                    }}
                  />
                )}
              </div>

              {/* 有数据：地区-工作点编号-班组 + 来源 */}
              {hasData && (
                <div style={{ marginTop: 2, lineHeight: 1.25, overflow: "hidden" }}>
                  <div
                    style={{
                      fontSize: 9,
                      color: "#555",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {dt!.region}
                  </div>
                  <div
                    style={{
                      fontSize: 9,
                      color: "#222",
                      fontWeight: 600,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {dt!.furnace}
                  </div>
                  <div style={{ fontSize: 9, color: "#888" }}>{dt!.squad ?? "非分班"}</div>
                  {dt!.source && (
                    <span
                      style={{
                        display: "inline-block",
                        marginTop: 1,
                        fontSize: 9,
                        color: "#fff",
                        background: srcColor,
                        borderRadius: 3,
                        padding: "0 4px",
                      }}
                    >
                      {dt!.source}
                    </span>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      )}

      {/* 详情 / 补录 弹层 */}
      <Popup
        visible={popup}
        onMaskClick={() => setPopup(false)}
        bodyStyle={{ padding: 16, borderRadius: "12px 12px 0 0", background: "#fff" }}
      >
        {selected && (
          <>
            <h3 style={{ marginTop: 0, marginBottom: 12 }}>
              {selected.date} 打卡详情
            </h3>
            {detail && detail.furnace ? (
              <div data-testid="cal-day-detail">
                {(() => {
                  // 每日详情按班次拆卡：基础出勤 + 每条加班次各一张卡；工作点信息放卡内（跨工作点加班可不同工作点）
                  const shifts = [
                    {
                      region: detail.region,
                      furnace: detail.furnace,
                      squad: detail.squad ?? null,
                      post: detail.post,
                      employType: detail.employType,
                      workHours: detail.workHours ?? 8,
                      overtime: false,
                    },
                    ...(detail.overtimes ?? []).map((o) => ({
                      region: o.region,
                      furnace: o.furnace,
                      squad: o.squad,
                      post: o.post,
                      employType: o.employType,
                      workHours: o.workHours,
                      overtime: true,
                    })),
                  ];
                  const ord = (sq: string | null) =>
                    sq === "1班" ? 1 : sq === "2班" ? 2 : sq === "3班" ? 3 : 9;
                  shifts.sort((a, b) => ord(a.squad) - ord(b.squad));
                  return (
                    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 12 }}>
                      {shifts.map((s, i) => {
                        const ot = Math.round(((s.workHours ?? 8) - 8) * 100) / 100;
                        return (
                          <div
                            key={i}
                            style={{
                              border: "1px solid #f0f0f0",
                              borderRadius: 8,
                              padding: "8px 10px",
                              background: s.overtime ? "#fff7f5" : "#fafafa",
                            }}
                          >
                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                              <span style={{ fontWeight: 600, color: "#333" }}>
                                {[s.region, s.furnace].filter(Boolean).join(" · ") || (s.squad ?? "不分班")}
                              </span>
                              {s.overtime && (
                                <span style={{ fontSize: 12, color: "#d4380d", background: "#fff2e8", borderRadius: 4, padding: "0 6px" }}>
                                  加班次
                                </span>
                              )}
                            </div>
                            <div style={{ fontSize: 13, color: "#666", marginTop: 2 }}>
                              {[s.squad ?? "不分班", s.post, s.employType].filter(Boolean).join(" · ")}
                            </div>
                            <div style={{ fontSize: 13, marginTop: 2, color: ot > 0 ? "#fa541c" : "#666" }}>
                              {ot > 0
                                ? `${s.workHours}小时（整班8h + 延时${ot}h）`
                                : s.workHours === 4
                                  ? "半班（4h）"
                                  : "整班（8h）"}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  );
                })()}
                <table style={{ width: "100%", fontSize: 14, borderCollapse: "collapse" }}>
                  <tbody>
                    {[
                      ["设备类型", detail.furnaceType || "—"],
                      ["值班长", detail.chief || "—"],
                      ["扫码时间", fmtTime(detail.scanTime)],
                      ["数据来源", detail.source || "—"],
                      ["确认状态", detail.confirmStatus || "—"],
                    ].map(([k, v]) => (
                      <tr key={k as string}>
                        <td
                          style={{
                            color: "#999",
                            padding: "6px 12px 6px 0",
                            width: 76,
                            verticalAlign: "top",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {k}
                        </td>
                        <td style={{ padding: "6px 0", color: "#222", fontWeight: 500 }}>{v}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Button block style={{ marginTop: 16 }} onClick={() => setPopup(false)}>
                  关闭
                </Button>
              </div>
            ) : (
              <div data-testid="cal-day-empty">
                <p style={{ color: "#888", fontSize: 14 }}>当日无工作点打卡记录</p>
                {selected.canMakeup ? (
                  <>
                    <p style={{ fontSize: 13, color: "#888", lineHeight: 1.7 }}>
                      补录与扫码上岗流程一致：先选择工作点（可直接扫工作点二维码，也可手动选择），
                      再依次选择班次 / 用工类型 / 是否半班 / 工种。提交后由值班长审核。
                    </p>
                    <Button block color="primary" onClick={goMakeup} data-testid="makeup-submit">
                      去补录：选择工作点与班次
                    </Button>
                    <Button block style={{ marginTop: 8 }} onClick={() => setPopup(false)}>
                      取消
                    </Button>
                  </>
                ) : (
                  <Button block style={{ marginTop: 8 }} onClick={() => setPopup(false)}>
                    关闭
                  </Button>
                )}
              </div>
            )}
          </>
        )}
      </Popup>
    </div>
  );
}
