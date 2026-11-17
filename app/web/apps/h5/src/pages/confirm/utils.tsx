import { FULL_SHIFT_HOURS } from "@fdc/api-client";
import { MAX_WORK_HOURS } from "./constants";

export function attendanceHoursLabel(v: string | null | undefined): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (Number.isFinite(n)) return `${Number(n.toFixed(2))}小时`;
  return `${v}小时`;
}

export function roundHours(h: number): number {
  return Math.round(h * 100) / 100;
}

export function validateWorkHours(wh: number): string | null {
  if (!Number.isFinite(wh) || wh < 0) return "请输入有效时长";
  if (wh > MAX_WORK_HOURS) return `时长不能超过 ${MAX_WORK_HOURS}h`;
  if (wh !== 0 && wh !== 4 && wh < FULL_SHIFT_HOURS) return "低于 8h 仅可选 4h（半班）或 0h（撤回），8~24h 可填写";
  return null;
}

export function capHoursInput(raw: string, maxHours = MAX_WORK_HOURS): string {
  if (raw === "") return raw;
  const wh = Number(raw);
  if (!Number.isFinite(wh)) return raw;
  if (wh > maxHours) return String(maxHours);
  if (wh < 0) return "0";
  return raw;
}

export function rosterTag(text: string, color: string, bg: string) {
  return (
    <span style={{ marginLeft: 6, fontSize: "0.85rem", color, background: bg, borderRadius: 4, padding: "0 4px" }}>
      {text}
    </span>
  );
}
