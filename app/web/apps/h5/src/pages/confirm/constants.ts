import { FULL_SHIFT_HOURS } from "@fdc/api-client";

export const SQUADLESS = "不分班";
export const MAX_WORK_HOURS = 24;
export const MAX_SHIFT_EXTENSION_HOURS = MAX_WORK_HOURS - FULL_SHIFT_HOURS;

export const ROSTER_SQUAD_TONE: Record<
  string,
  { bg: string; border: string; text: string; activeBg: string }
> = {
  "1班": { bg: "#f5f8fb", border: "#d6e3ee", text: "#31556f", activeBg: "#eaf2f8" },
  "2班": { bg: "#f7f8f1", border: "#dce5c9", text: "#526238", activeBg: "#eef4e4" },
  "3班": { bg: "#faf5f1", border: "#ead7ca", text: "#70523f", activeBg: "#f4ebe4" },
};
