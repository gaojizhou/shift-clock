import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavBar, Toast } from "antd-mobile";
import {
  CameraOutline,
  CheckShieldOutline,
  ContentOutline,
  CalendarOutline,
  AppstoreOutline,
  SetOutline,
  RightOutline,
} from "antd-mobile-icons";
import { useNavigate } from "react-router-dom";
import { apiGet, getDemoContext, workHoursLabel } from "@fdc/api-client";
import { inDingTalk } from "../dingtalk";
import { useAuth } from "../auth";

/** 适老化大入口卡：图标 + 大标题 + 副提示，整块可点、触控区大 */
function EntryCard({
  icon,
  title,
  desc,
  color,
  onClick,
  testid,
}: {
  icon: ReactNode;
  title: string;
  desc?: string;
  color: string;
  onClick: () => void;
  testid?: string;
}) {
  return (
    <div
      data-testid={testid}
      onClick={onClick}
      role="button"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.9rem",
        background: "#fff",
        borderRadius: "0.8rem",
        padding: "1.1rem 1rem",
        marginBottom: "0.8rem",
        boxShadow: "0 1px 6px rgba(0,0,0,0.05)",
        cursor: "pointer",
      }}
    >
      <div
        style={{
          width: "3.2rem",
          height: "3.2rem",
          borderRadius: "0.7rem",
          background: color + "1a",
          color,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: "1.8rem",
          flexShrink: 0,
        }}
      >
        {icon}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: "1.25rem", fontWeight: 700, color: "#1a1a1a" }}>{title}</div>
        {desc ? (
          <div style={{ fontSize: "0.9rem", color: "#888", marginTop: "0.2rem", lineHeight: 1.4 }}>
            {desc}
          </div>
        ) : null}
      </div>
      <RightOutline fontSize="1.3rem" color="#ccc" />
    </div>
  );
}

type TodayDetail = {
  furnace?: string;
  region?: string;
  post?: string;
  squad?: string | null;
  employType?: string;
  workHours?: number;
  scanTime?: string | null;
  source?: string | null;
  overtimes?: {
    furnace?: string;
    region?: string;
    post: string;
    squad: string | null;
    employType: string;
    workHours: number;
  }[];
};
type AttendanceDay = { date: string; status: string; detail?: TodayDetail | null };

const TODAY_STATUS_COLOR: Record<string, string> = {
  正常: "#52c41a",
  待审: "#faad14",
  异常: "#fa8c16",
  已确认: "#1677ff",
};
const fmtScanTime = (iso?: string | null) => (iso ? iso.replace("T", " ").slice(0, 16) : "—");

export default function HomePage() {
  const nav = useNavigate();
  const { me } = useAuth();
  const role = me.role;
  const [todayDetail, setTodayDetail] = useState<TodayDetail | null>(null);
  const [todayStatus, setTodayStatus] = useState("");
  const regionNotice = (role === "hr" || role === "stat") && me.requiresRegionSelection
    ? me.regionMessage
      || (me.regionConflict
        ? "当前账号配置了多个地区，请进入管理端收敛为一个地区后再操作"
        : "当前账号尚未配置地区，请进入管理端选择地区后再操作")
    : "";

  useEffect(() => {
    if (me.userid) localStorage.setItem("fdc_userid", me.userid);
  }, [me.userid]);

  // 今日打卡：若本人今天已有工作点打卡记录，主操作区顶部展示一张概览卡片
  useEffect(() => {
    let ignore = false;
    (async () => {
      try {
        const ctx = await getDemoContext();
        if (ignore || !me.userid) return;
        const att = await apiGet<{ days: AttendanceDay[] }>(
          `/attendance?userid=${encodeURIComponent(me.userid)}&month=${ctx.month}`
        );
        if (ignore) return;
        const td = att.days.find((d) => d.date === ctx.today);
        setTodayDetail(td?.detail ?? null);
        setTodayStatus(td?.status ?? "");
      } catch {
        /* 今日打卡卡片为附加信息，失败静默不影响首页 */
      }
    })();
    return () => {
      ignore = true;
    };
  }, [me.userid]);

  // 隐藏入口：连续快速点击标题 7 次进入调试页
  const tapRef = useRef<{ n: number; t: number }>({ n: 0, t: 0 });
  const onTitleTap = () => {
    const now = Date.now();
    const prev = tapRef.current;
    const n = now - prev.t < 1500 ? prev.n + 1 : 1;
    tapRef.current = { n, t: now };
    if (n >= 7) {
      tapRef.current = { n: 0, t: 0 };
      nav("/debug");
    } else if (n >= 4) {
      Toast.show({ content: `再点 ${7 - n} 次进入调试`, duration: 700 });
    }
  };

  const isChief = role === "chief";
  // 可进入管理后台的角色：人事 / 统计 / 管理员（值班长在 H5 完成全部工作，不进后台）
  const canEnterAdmin = role === "hr" || role === "stat" || role === "admin";
  const isDD = inDingTalk();

  // 桌面管理端与 H5 同源部署（admin 在 / ，H5 在 /h5），会话 cookie 共享；
  // 本地开发时两端是不同 vite 端口，跳 admin 的 5173。
  const openDesktop = () => {
    const url = import.meta.env.DEV
      ? `${window.location.protocol}//${window.location.hostname}:5173/`
      : "/";
    window.location.href = url;
  };

  return (
    <div style={{ background: "#f5f5f5", minHeight: "100vh" }}>
      <NavBar
        style={{ background: "#1677FF", color: "#fff" }}
        back={null}
        right={
          <SetOutline
            data-testid="enter-settings"
            fontSize={20}
            style={{ color: "#fff", opacity: 0.85, cursor: "pointer" }}
            onClick={() => nav("/settings")}
          />
        }
      >
        <span data-testid="home-title-debug" onClick={onTitleTap}>
          工作点打卡
        </span>
      </NavBar>

      {regionNotice ? (
        <div style={{ margin: 12, padding: 12, color: "#873800", background: "#fff7e6", border: "1px solid #ffd591", borderRadius: 8 }}>
          {regionNotice}
        </div>
      ) : null}

      {/* 顶部：用户信息 / 非钉钉提示 */}
      {isDD ? (
        <div
          data-testid="dd-user-card"
          style={{ margin: 12, padding: 16, background: "#fff", borderRadius: 8 }}
        >
          <div style={{ fontSize: 13, color: "#999", marginBottom: 8 }}>当前用户（钉钉）</div>
          <div>
            <div style={{ fontSize: 20, fontWeight: 600 }}>
              {me.name || "（未配置姓名）"}
            </div>
            <div style={{ fontSize: 13, color: me.title ? "#666" : "#999", marginTop: 6 }}>
              职务：{me.title || "未同步"}
            </div>
          </div>
        </div>
      ) : (
        <div
          data-testid="non-dd-tip"
          style={{
            margin: 12,
            padding: 16,
            background: "#fffbe6",
            border: "1px solid #ffe58f",
            borderRadius: 8,
          }}
        >
          <div style={{ fontSize: 15, fontWeight: 600, color: "#ad6800", marginBottom: 6 }}>
            请在钉钉中打开
          </div>
          <div style={{ fontSize: 13, color: "#8c6e1f", lineHeight: 1.6 }}>
            当前不是钉钉环境，无法获取用户信息。请通过<strong>钉钉工作台</strong>找到本应用入口打开使用。
          </div>
        </div>
      )}

      {/* 主操作：图标 + 大字卡片，方便老年人识别与点选 */}
      {role && (
        <div style={{ padding: "0.4rem 0.8rem" }}>
          <div style={{ fontSize: "0.95rem", color: "#888", margin: "0.6rem 0.2rem" }}>主操作</div>
          {/* 今日打卡：本人今天已有工作点打卡记录时展示概览卡片，点按进工作点打卡记录看详情 */}
          {todayDetail?.furnace && (
            <div
              data-testid="today-scan-card"
              role="button"
              onClick={() => nav("/calendar")}
              style={{
                background: "#fff",
                borderRadius: "0.8rem",
                padding: "1rem",
                marginBottom: "0.8rem",
                boxShadow: "0 1px 6px rgba(0,0,0,0.05)",
                cursor: "pointer",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: "0.5rem",
                }}
              >
                <span style={{ fontSize: "1.2rem", fontWeight: 700, color: "#1a1a1a" }}>
                  今日打卡
                </span>
                <span
                  style={{
                    fontSize: "0.85rem",
                    fontWeight: 600,
                    color: TODAY_STATUS_COLOR[todayStatus] || "#52c41a",
                    background: (TODAY_STATUS_COLOR[todayStatus] || "#52c41a") + "1a",
                    padding: "0.15rem 0.7rem",
                    borderRadius: "0.8rem",
                  }}
                >
                  {todayStatus || "已上岗"}
                </span>
              </div>
              {(() => {
                // 今日打卡按班次拆卡：基础出勤 + 每条加班次各一张卡；工作点信息放卡内（跨工作点加班可不同工作点）
                const shifts = [
                  {
                    region: todayDetail.region,
                    furnace: todayDetail.furnace,
                    squad: todayDetail.squad ?? null,
                    post: todayDetail.post,
                    employType: todayDetail.employType,
                    workHours: todayDetail.workHours ?? 8,
                    overtime: false,
                  },
                  ...(todayDetail.overtimes ?? []).map((o) => ({
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
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", marginTop: "0.5rem" }}>
                    {shifts.map((s, i) => {
                      const ot = Math.round(((s.workHours ?? 8) - 8) * 100) / 100;
                      return (
                        <div
                          key={i}
                          style={{
                            border: "1px solid #f0f0f0",
                            borderRadius: "0.6rem",
                            padding: "0.5rem 0.7rem",
                            background: s.overtime ? "#fff7f5" : "#fafafa",
                          }}
                        >
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                            <span style={{ fontWeight: 600, color: "#333" }}>
                              {[s.region, s.furnace].filter(Boolean).join(" · ") || (s.squad ?? "不分班")}
                            </span>
                            {s.overtime && (
                              <span style={{ fontSize: "0.72rem", color: "#d4380d", background: "#fff2e8", borderRadius: 4, padding: "0 6px" }}>
                                加班次
                              </span>
                            )}
                          </div>
                          <div style={{ fontSize: "0.9rem", color: "#666", marginTop: "0.15rem" }}>
                            {[s.squad ?? "不分班", s.post, s.employType].filter(Boolean).join(" · ")}
                          </div>
                          <div style={{ fontSize: "0.9rem", marginTop: "0.15rem", color: ot > 0 ? "#fa541c" : "#666" }}>
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
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginTop: "0.5rem",
                }}
              >
                <span style={{ fontSize: "0.85rem", color: "#999" }}>
                  {fmtScanTime(todayDetail.scanTime)} · {todayDetail.source || "扫码"}
                </span>
                <span style={{ fontSize: "0.85rem", color: "#1677FF", display: "flex", alignItems: "center" }}>
                  打卡详情 <RightOutline />
                </span>
              </div>
            </div>
          )}
          {/* 扫码上岗 / 工作点打卡记录：所有角色均可使用 */}
          <EntryCard
            testid="scan-entry"
            icon={<CameraOutline />}
            title="扫码上岗"
            desc="扫工作点二维码，选班次工种登记上岗"
            color="#1677FF"
            onClick={() => nav("/scan-sim")}
          />
          {isChief && (
            <EntryCard
              icon={<CheckShieldOutline />}
              title="值班长日确认"
              desc="核对本工作点编制/已扫/缺编，确认锁定当日"
              color="#52c41a"
              onClick={() => nav("/confirm")}
            />
          )}
          {isChief && (
            <EntryCard
              testid="chief-review-entry"
              icon={<ContentOutline />}
              title="值班长审核确认"
              desc="补录待审 + HR 转交「待值班长确认」"
              color="#fa8c16"
              onClick={() => nav("/chief-review")}
            />
          )}
          <EntryCard
            testid="calendar-entry"
            icon={<CalendarOutline />}
            title="工作点打卡记录"
            desc="查看本人出勤明细，对漏扫日发起补录"
            color="#722ed1"
            onClick={() => nav("/calendar")}
          />
        </div>
      )}

      {/* 页面底部：人事 / 统计 / 值班长 / 管理员可进入管理后台 */}
      {canEnterAdmin && (
        <div style={{ padding: "0.4rem 0.8rem 1.2rem" }}>
          <EntryCard
            testid="enter-admin"
            icon={<AppstoreOutline />}
            title="进入管理后台"
            desc="编制 / 字典 / 异常 / 导出，电脑访问体验更佳"
            color="#13c2c2"
            onClick={openDesktop}
          />
        </div>
      )}

    </div>
  );
}
