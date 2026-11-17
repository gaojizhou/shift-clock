import { Tag, Tooltip } from "antd";
import dayjs from "dayjs";
import {
  type ReconTimelineItem,
  type SquadScheduleItem,
  type TimelineOvertime,
} from "@fdc/api-client";

/** 对账状态 → Tag 颜色（班次覆盖判定口径）。考勤对照/劳勤工作台共用。 */
export const RECON_STATUS_COLOR: Record<string, string> = {
  自动一致: "green",
  时间错位: "orange",
  劳勤有工作点无: "red",
  工作点有劳勤无: "volcano",
  无数据: "default",
};
export const RECON_STATUS_OPTIONS = Object.keys(RECON_STATUS_COLOR);

/** 班次背景淡色 */
const BAND_COLOR: Record<string, string> = {
  "早班": "rgba(22,119,255,0.06)",
  "中班": "rgba(250,140,22,0.08)",
  "晚班": "rgba(114,46,209,0.06)",
};

/** 班次段高亮按数据来源着色：自己扫码=绿，补录通过=青，值班长代签=紫，补录待审=金 */
const ORIGIN_STYLE: Record<string, { color: string; text: string; label: string }> = {
  扫码: { color: "82,196,26", text: "#237804", label: "扫码" },
  补录通过: { color: "19,194,194", text: "#006d75", label: "补录通过" },
  代签: { color: "114,46,209", text: "#391085", label: "值班长代签" },
  补录待审: { color: "250,173,20", text: "#ad6800", label: "补录待审" },
  加班: { color: "212,56,13", text: "#ad2102", label: "加班" },
};

/** 不分班岗位（值班长/维修工等）独立配色：品红 */
const SQUADLESS_COLOR = "235,47,150";
export const SQUADLESS_TEXT = "#9e1068";

// 时间轴范围：三班倒跨 0 点，前后各多展示 4 小时（前日20:00 ~ 次日04:00）
const AXIS_START = -4 * 60;
const AXIS_END = 28 * 60;
const AXIS_SPAN = AXIS_END - AXIS_START;
const HOUR_TICKS = [-4, 0, 4, 8, 12, 16, 20, 24, 28];

function tickLabel(h: number): string {
  if (h < 0) return `前日${h + 24}`;
  if (h > 24) return `次日${h - 24}`;
  return String(h);
}
function pctOfMin(min: number): number {
  return ((min - AXIS_START) / AXIS_SPAN) * 100;
}
/** 相对展示日期 0 点的分钟数：跨日时间点（如次日凌晨下班卡）自然落在 24h 之外 */
function minOfIso(iso: string, date: string): number {
  return dayjs(iso).diff(dayjs(date, "YYYY-MM-DD").startOf("day"), "minute");
}
function minOfHHmm(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return (h % 24) * 60 + (m || 0);
}
/** 班次时间段 → [startMin, endMin]（跨午夜则 end+24h） */
function scheduleInterval(w: SquadScheduleItem): [number, number] {
  const s = minOfHHmm(w.from);
  let e = minOfHHmm(w.to);
  if (w.crossesMidnight || e <= s) e += 1440;
  return [s, e];
}
function fullDt(iso: string): string {
  return dayjs(iso).format("YYYY-MM-DD HH:mm");
}
function inlineDt(iso: string, date: string): string {
  const t = dayjs(iso);
  const diff = t.startOf("day").diff(dayjs(date, "YYYY-MM-DD").startOf("day"), "day");
  const hm = t.format("HH:mm");
  if (diff === 1) return `次日${hm}`;
  if (diff === -1) return `前日${hm}`;
  return hm;
}
function clipToAxis(s: number, e: number): [number, number] | null {
  const cs = Math.max(s, AXIS_START);
  const ce = Math.min(e, AXIS_END);
  if (ce <= cs) return null;
  return [pctOfMin(cs), ((ce - cs) / AXIS_SPAN) * 100];
}
function punchGlyph(inoutType: string): { glyph: string; label: string } {
  if (/进|上班/.test(inoutType)) return { glyph: "▶", label: "上班卡" };
  if (/出|下班/.test(inoutType)) return { glyph: "◀", label: "下班卡" };
  return { glyph: "●", label: inoutType || "方向未知" };
}

/** 顶部小时刻度尺（与时间轴同宽对齐） */
export function HourRuler() {
  return (
    <div style={{ position: "relative", height: 18 }}>
      {HOUR_TICKS.map((h) => (
        <span
          key={h}
          style={{
            position: "absolute",
            left: `${pctOfMin(h * 60)}%`,
            transform: "translateX(-50%)",
            fontSize: 11,
            color: h === 0 || h === 24 ? "#555" : "#999",
            fontWeight: h === 0 || h === 24 ? 600 : 400,
          }}
        >
          {tickLabel(h)}
        </span>
      ))}
    </div>
  );
}

/** 图例（与时间轴含义一致）。 */
export function TimelineLegend() {
  return (
    <span style={{ fontSize: 12, color: "#999" }}>
      图例：<span style={{ color: "#52c41a" }}>■ 扫码</span>{" "}
      <span style={{ color: "#13c2c2" }}>■ 补录通过</span>{" "}
      <span style={{ color: "#722ed1" }}>■ 值班长代签</span>{" "}
      <span style={{ color: "#faad14" }}>▨ 补录待审</span>{" "}
      <span style={{ color: SQUADLESS_TEXT }}>■ 不分班</span>{" "}
      <span style={{ color: "#d4380d" }}>■ 加班</span>{" "}
      <span style={{ color: "#666" }}>框长=实际工时</span>{" "}
      <span style={{ color: "#389e0d" }}>▼ 扫码时刻</span>{" "}
      <span style={{ color: "#1677ff" }}>▶ 上班卡 ◀ 下班卡 ● 方向未知</span>
    </span>
  );
}

/** 独立加班核对细时间线：完整展示“申报区间前12h → 申报加班区间 → 区间后12h”和窗口内打卡。 */
export function OvertimeCheckLine({ overtime }: { overtime: TimelineOvertime }) {
  const check = overtime.checkWindow;
  if (!check?.windowStart || !check.windowEnd || !check.shiftStart || !check.shiftEnd) {
    return (
      <div
        data-testid={`overtime-check-window-${overtime.id}`}
        style={{ padding: "8px 10px", marginBottom: 8, background: "#fff7e6", borderRadius: 4, color: "#ad6800" }}
      >
        未找到“{overtime.shiftSlot || "未选择"}”的班次时间，无法生成加班取卡窗口。
      </div>
    );
  }

  const start = dayjs(check.windowStart);
  const end = dayjs(check.windowEnd);
  const span = Math.max(1, end.valueOf() - start.valueOf());
  const pct = (iso: string) => Math.max(0, Math.min(100, ((dayjs(iso).valueOf() - start.valueOf()) / span) * 100));
  const shiftLeft = pct(check.shiftStart);
  const shiftWidth = Math.max(1, pct(check.shiftEnd) - shiftLeft);
  const short = (iso: string) => dayjs(iso).format("MM-DD HH:mm");

  return (
    <div
      data-testid={`overtime-check-window-${overtime.id}`}
      style={{ marginBottom: 10, padding: "8px 12px 6px", border: "1px solid #ffe7ba", borderRadius: 6, background: "#fffdf8" }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", fontSize: 12 }}>
        <strong>加班取卡窗口</strong>
        <Tag color={check.covered ? "green" : "purple"}>{check.covered ? "打卡完整覆盖申报区间" : "打卡未完整覆盖申报区间"}</Tag>
        <span style={{ color: "#666" }}>
          {short(check.windowStart)} 至 {short(check.windowEnd)}（申报区间前后各 12 小时）
        </span>
      </div>
      <div style={{ position: "relative", height: 82, margin: "4px 22px 0" }}>
        {/* 取卡窗口用一条细线贯穿；橙色粗段表示真正需要覆盖的申报加班区间。 */}
        <div style={{ position: "absolute", left: 0, right: 0, top: 30, borderTop: "1px solid #8c8c8c" }} />
        <Tooltip title={`申报加班区间：${short(check.shiftStart)} 至 ${short(check.shiftEnd)}`}>
          <div
            data-testid={`overtime-shift-segment-${overtime.id}`}
            style={{
              position: "absolute",
              left: `${shiftLeft}%`,
              width: `${shiftWidth}%`,
              top: 27,
              height: 7,
              borderRadius: 4,
              background: "#fa8c16",
              boxShadow: "0 0 0 1px rgba(250,140,22,.18)",
              zIndex: 1,
            }}
          />
        </Tooltip>
        {[
          { left: 0, label: `窗口开始 ${short(check.windowStart)}`, align: "translateX(0)" },
          { left: 100, label: `窗口结束 ${short(check.windowEnd)}`, align: "translateX(-100%)" },
        ].map((point) => (
          <div key={point.label} style={{ position: "absolute", left: `${point.left}%`, top: 19, transform: point.align, zIndex: 2 }}>
            <div style={{ height: 22, borderLeft: "1px solid #595959" }} />
            <div style={{ marginTop: 1, fontSize: 10, color: "#777", whiteSpace: "nowrap" }}>{point.label}</div>
          </div>
        ))}
        {(check.punches ?? []).map((p, i) => {
          const { glyph, label } = punchGlyph(p.inoutType);
          const selected = !!p.selectedForCalculation;
          const calculationLabel = p.calculationRole === "start" ? "计算进卡" : "计算出卡";
          return (
            <Tooltip
              key={`${p.id ?? p.time}-${i}`}
              title={`${selected ? `${calculationLabel} · ` : ""}${label}：${fullDt(p.time)}（原值：${p.inoutType}）`}
            >
              <div
                data-testid={`overtime-window-punch-${overtime.id}-${i}`}
                data-selected-for-calculation={selected ? "true" : "false"}
                style={{
                  position: "absolute",
                  left: `${pct(p.time)}%`,
                  top: selected ? 0 : 9,
                  transform: "translateX(-50%)",
                  textAlign: "center",
                  color: selected ? "#ad6800" : "#1677ff",
                  whiteSpace: "nowrap",
                  zIndex: selected ? 5 : 3,
                }}
              >
                <div style={{ fontSize: 10, lineHeight: 1.1, fontWeight: selected ? 700 : 400 }}>
                  {dayjs(p.time).format("MM-DD HH:mm")}
                </div>
                <div
                  style={selected ? {
                    display: "inline-block",
                    minWidth: 22,
                    padding: "1px 4px",
                    border: "2px solid #faad14",
                    borderRadius: 10,
                    background: "#fffbe6",
                    boxShadow: "0 0 0 2px rgba(250,173,20,.16)",
                    fontSize: 13,
                    lineHeight: 1,
                  } : { fontSize: 13, lineHeight: 1 }}
                >
                  {selected ? "★" : ""}{glyph}
                </div>
                {selected && (
                  <div style={{ marginTop: 1, fontSize: 9, lineHeight: 1, fontWeight: 700 }}>计算选卡</div>
                )}
              </div>
            </Tooltip>
          );
        })}
        <div
          style={{
            position: "absolute",
            left: `${shiftLeft + shiftWidth / 2}%`,
            top: 51,
            transform: "translateX(-50%)",
            fontSize: 10,
            color: "#ad4e00",
            whiteSpace: "nowrap",
          }}
        >
          {overtime.shiftSlot} {short(check.shiftStart)} → {short(check.shiftEnd)}
        </div>
      </div>
      <div
        data-testid={`overtime-window-punch-list-${overtime.id}`}
        style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginTop: 2, marginBottom: 4 }}
      >
        <span style={{ color: "#666", fontSize: 11 }}>窗口内全部打卡（{check.punches?.length ?? 0}）：</span>
        {(check.punches ?? []).map((p, i) => {
          const { glyph, label } = punchGlyph(p.inoutType);
          const selected = !!p.selectedForCalculation;
          return (
            <Tag
              key={`list-${p.id ?? p.time}-${i}`}
              color={selected ? "gold" : "blue"}
              style={{ marginInlineEnd: 0, fontWeight: selected ? 700 : 400 }}
            >
              {selected ? "★ 计算选卡 · " : ""}{dayjs(p.time).format("MM-DD HH:mm:ss")} {glyph} {label}
            </Tag>
          );
        })}
        {(check.punches?.length ?? 0) === 0 && <span style={{ color: "#999", fontSize: 11 }}>无</span>}
      </div>
      <div style={{ fontSize: 11, color: "#8c8c8c" }}>
        细线＝允许取卡范围　橙色段＝必须完整覆盖的申报加班区间　蓝色＝窗口内原始卡　金色★＝覆盖计算选卡
      </div>
    </div>
  );
}

/** 单条记录的 32h 双轨时间轴图（班次背景带 + 班次段高亮 + 扫码▼ + 钉钉打卡）。
 *  从原 AttendanceCompare 的 TimelineRow 右侧轴抽取，供考勤对照与劳勤工作台共用。 */
export function TimelineAxis({
  item,
  windows,
  date,
}: {
  item: ReconTimelineItem;
  windows: SquadScheduleItem[];
  date: string;
}) {
  const byName = new Map(windows.map((w) => [w.squad, w]));
  const blockCount = item.overtimes?.length ?? 0;
  const rowHeight = 84 + Math.max(0, blockCount - 1) * 28;
  // 该行涉及多个工作点时（跨工作点加班），各班次框前缀工作点编号以区分；单工作点则不加前缀避免冗余
  const rowFurnaces = new Set(
    [
      ...item.scans,
      ...(item.pendingMakeups ?? []),
      ...(item.overtimes ?? []),
    ]
      .map((b) => b.furnace)
      .filter(Boolean)
  );
  const multiFurnace = rowFurnaces.size > 1;
  return (
    <div style={{ flex: 1, position: "relative", minHeight: rowHeight, overflow: "hidden" }}>
      {/* 三班背景淡色带：±24h 平移补齐轴两端 */}
      {windows.flatMap((w) => {
        const [s, e] = scheduleInterval(w);
        return [-1440, 0, 1440].flatMap((off) => {
          const seg = clipToAxis(s + off, e + off);
          if (!seg) return [];
          return (
            <div
              key={`bg-${w.squad}-${off}`}
              title={`${w.squad} ${w.from}-${w.to}${w.crossesMidnight ? "(次日)" : ""}`}
              style={{
                position: "absolute",
                left: `${seg[0]}%`,
                width: `${seg[1]}%`,
                top: 0,
                bottom: 0,
                background: BAND_COLOR[w.squad] || "rgba(0,0,0,0.03)",
              }}
            />
          );
        });
      })}
      {[0, 1440].map((m) => (
        <div
          key={m}
          style={{
            position: "absolute",
            left: `${pctOfMin(m)}%`,
            top: 0,
            bottom: 0,
            borderLeft: "1px dashed #d9d9d9",
          }}
        />
      ))}
      {/* 加班时段高亮 */}
      {(item.overtimes ?? []).map((o) => ({
          time: o.time ?? null,
          region: o.region,
          furnace: o.furnace,
          squad: o.squad,
          shiftSlot: o.shiftSlot,
          post: o.post,
          employType: o.employType,
          source: null as string | null,
          workHours: o.workHours,
          _origin: "加班",
          _pending: false,
          _overtime: true,
        })).map((s, i) => {
        const w = byName.get(s.shiftSlot ?? "");
        if (!w) return null;
        const interval = scheduleInterval(w);
        const shiftMin = interval[1] - interval[0];
        const wh = s.workHours ?? 8;
        // 框框长度 = 加班实际工时，从时段起点向后铺。
        const endMin = interval[0] + wh * 60;
        const seg = clipToAxis(interval[0], endMin);
        if (!seg) return null;
        const overHours = Math.round(Math.max(0, wh - shiftMin / 60) * 100) / 100;
        const baseStyle = ORIGIN_STYLE[s._origin] || ORIGIN_STYLE["扫码"];
        const st = baseStyle;
        const otSuffix = overHours > 0 ? `·超时段+${overHours}h` : "·加班";
        const furnacePrefix = multiFurnace && s.furnace ? `${s.furnace}·` : "";
        const label = `${furnacePrefix}${s.shiftSlot} ${s.squad ?? "不分班"} ${s.post}·${s.employType} · ${wh}h${otSuffix}`;
        const endDate = w.crossesMidnight
          ? dayjs(date, "YYYY-MM-DD").add(1, "day").format("YYYY-MM-DD")
          : date;
        const dashed = s._pending;
        return (
          <Tooltip
            key={`hl${i}`}
            title={`${st.label} ｜ 时段 ${s.shiftSlot}（${date} ${w.from} ~ ${endDate} ${w.to}）｜ 班组 ${s.squad ?? "不分班"} ｜ ${s.region ?? ""}${s.furnace ?? ""} ${s.post} ${s.employType} ｜ 工时 ${wh}h`}
          >
            <div
              data-testid={`squad-hl-${item.empId}-${i}`}
              style={{
                position: "absolute",
                left: `${seg[0]}%`,
                width: `${seg[1]}%`,
                top: 24 + i * 28,
                height: 26,
                borderRadius: 4,
                border: dashed ? `1.5px dashed rgb(${st.color})` : `1px solid rgb(${st.color})`,
                background: s._pending
                  ? `rgba(${st.color},0.12)`
                  : `rgba(${st.color},0.22)`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 11,
                color: st.text,
                whiteSpace: "nowrap",
                overflow: "hidden",
                zIndex: 1,
                cursor: "default",
              }}
            >
              {label}
            </div>
          </Tooltip>
        );
      })}
      {/* 扫码发生时刻 ▼ */}
      {item.scans.map((s, i) => (
        <Tooltip
          key={`s${i}`}
          title={`扫码时间 ${fullDt(s.time)}${s.source ? `（${s.source}）` : ""} ｜ 选定 ${s.squad ?? "-"}（${date}）`}
        >
          <div
            data-testid={`scan-pt-${item.empId}-${i}`}
            style={{
              position: "absolute",
              left: `${pctOfMin(minOfIso(s.time, date))}%`,
              top: 2,
              transform: "translateX(-50%)",
              textAlign: "center",
              whiteSpace: "nowrap",
              cursor: "default",
              zIndex: 2,
              lineHeight: 1,
            }}
          >
            <div style={{ fontSize: 9, color: "#73a35c" }}>{dayjs(s.time).format("HH:mm")}</div>
            <span style={{ color: "#389e0d", fontSize: 8 }}>▼</span>
          </div>
        </Tooltip>
      ))}
      {/* 下轨：钉钉打卡 */}
      {item.punches.map((p, i) => {
        const t = inlineDt(p.time, date);
        const { glyph, label } = punchGlyph(p.inoutType);
        return (
          <Tooltip
            key={`p${i}`}
            title={`钉钉 ${fullDt(p.time)} ｜ ${label}（原值：${p.inoutType}）${p.source ? ` 来源：${p.source}` : ""}`}
          >
            <div
              data-testid={`punch-pt-${item.empId}-${i}`}
              style={{
                position: "absolute",
                left: `${pctOfMin(minOfIso(p.time, date))}%`,
                bottom: 3,
                transform: "translateX(-50%)",
                textAlign: "center",
                whiteSpace: "nowrap",
                cursor: "default",
                zIndex: 2,
              }}
            >
              <div style={{ fontSize: 11, color: "#1677ff", lineHeight: 1.2 }}>{t}</div>
              <span style={{ color: "#1677ff", fontSize: 13, lineHeight: 1 }}>{glyph}</span>
            </div>
          </Tooltip>
        );
      })}
    </div>
  );
}
