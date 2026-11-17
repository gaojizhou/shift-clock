import { SQUADS } from "@fdc/api-client";

export type Person = {
  empId: string;
  name?: string;
  post?: string;
  squad?: string | null;
  employType?: string;
  workHours?: number;
  excluded?: boolean;
  boundToChief?: boolean;
  boundToAnyChief?: boolean;
  overtime?: boolean;
  overtimeId?: number;
};

export type Shortage = {
  post: string;
  squad: string | null;
  capacity: number;
  actual: number;
  shortage: number;
};

export type OvertimeCandidate = {
  empId: string;
  name?: string;
  employmentStatus: "active" | "departed";
  squads: string[];
  baseSquad?: string | null;
  slots?: string[];
  usedHours?: number;
  remainingHours?: number;
};

export type Sections = {
  scanned: Person[];
  notScanned: Person[];
  external: Person[];
  pendingMakeup: { empId: string; name?: string; id?: number }[];
  reviewQueue: Person[];
  shortage: Shortage[];
  chiefs?: { empId: string; name?: string | null }[];
  overtimeCandidates: OvertimeCandidate[];
  overtime: {
    id: number;
    empId: string;
    name?: string;
    post: string;
    squad: string | null;
    shiftSlot?: string | null;
    employType: string;
    workHours: number;
  }[];
  locked: boolean;
  confirmedBy: string | null;
};

export type Evidence = {
  empId: string;
  date: string;
  furnace: string;
  status: string;
  timeDiffMinutes: number | null;
  attendanceResultWorkHours?: string | null;
  laoqinPunches: { time: string; inoutType: string; source: string | null; makeupReason?: string | null }[];
  scanRecords: {
    time: string;
    post: string;
    squad: string | null;
    shiftSlot?: string | null;
    employType: string;
    source: string;
    workHours?: number;
  }[];
  overtimes?: {
    post: string;
    squad: string | null;
    employType: string;
    workHours: number;
    approver?: string | null;
  }[];
};

export type RosterGroup = { post: string; capacity: number; formal: Person[]; extras: Person[] };
export type SquadName = (typeof SQUADS)[number];
