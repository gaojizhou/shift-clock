import { Button } from "antd-mobile";
import { isHalfShift, workHoursLabel } from "@fdc/api-client";
import type { Person } from "../types";
import { rosterTag, roundHours } from "../utils";

export function RosterPerson({
  p,
  onPick,
  onBind,
}: {
  p: Person;
  onPick: (p: Person) => void;
  onBind: (empId: string) => void;
}) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", flexWrap: "wrap" }}>
      <span data-testid={`roster-${p.empId}`} style={{ color: "#1677FF", cursor: "pointer" }} onClick={() => onPick(p)}>
        {p.name ?? p.empId}
      </span>
      {p.employType && p.employType !== "正式"
        ? rosterTag(p.employType, "#fa8c16", "#fff7e6")
        : rosterTag("正式", "#389e0d", "#f6ffed")}
      {p.overtime ? rosterTag("多班次", "#d4380d", "#fff2e8") : null}
      {(p.workHours ?? 8) > 8
        ? rosterTag(`延时+${roundHours((p.workHours ?? 8) - 8)}h`, "#fa541c", "#fff7e6")
        : null}
      {!p.overtime && isHalfShift(p.workHours)
        ? rosterTag(workHoursLabel(p.workHours, { day: true }), "#fa8c16", "#fff7e6")
        : null}
      {!p.boundToChief && rosterTag("外来", "#cf1322", "#fff1f0")}
      {!p.boundToChief && !p.boundToAnyChief && (
        <Button size="mini" color="primary" fill="outline" style={{ marginLeft: 6 }} onClick={() => onBind(p.empId)}>
          绑定该操作工
        </Button>
      )}
    </span>
  );
}
