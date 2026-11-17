import { Button } from "antd-mobile";
import type { Person, RosterGroup } from "../types";
import { roundHours } from "../utils";
import { RosterPerson } from "./RosterPerson";

export function RosterPostGroup({
  g,
  squad,
  onPick,
  onProxy,
  onBind,
  mutationsDisabled = false,
}: {
  g: RosterGroup;
  squad: string | null;
  onPick: (p: Person) => void;
  onProxy: (post: string, squad: string | null) => void;
  onBind: (empId: string) => void;
  mutationsDisabled?: boolean;
}) {
  const occupied = roundHours(g.formal.reduce((s, p) => s + Math.min(p.workHours ?? 8, 8) / 8, 0));
  const short = roundHours(g.capacity - occupied);
  const fractional = Math.abs(occupied - g.formal.length) > 0.01 || short > 0.01;
  const emptyWhole = Math.max(0, g.capacity - g.formal.length);
  const extraFractionalSlot = short > 0.01 && emptyWhole === 0 ? 1 : 0;
  const rows = g.formal.length + emptyWhole + extraFractionalSlot;

  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontWeight: 600, fontSize: "1rem" }}>
        {g.post}
        <span style={{ color: "#999", fontWeight: 400, fontSize: "0.92rem", marginLeft: 6 }}>
          正式岗 {g.formal.length}/{g.capacity}人
          {fractional && (
            <span style={{ color: short > 0.01 ? "#cf1322" : "#999", marginLeft: 4 }}>
              · 占 {occupied}/{g.capacity} 位{short > 0.01 ? ` · 缺 ${short}` : ""}
            </span>
          )}
        </span>
      </div>
      {Array.from({ length: rows }).map((_, i) => {
        const p = g.formal[i];
        return (
          <div
            key={`f${i}`}
            style={{
              marginLeft: 16,
              padding: "4px 0",
              borderBottom: "1px dashed #f0f0f0",
              fontSize: "1rem",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            {p ? (
              <RosterPerson p={p} onPick={onPick} onBind={onBind} />
            ) : (
              <>
                <span style={{ color: "#bbb" }}>正式岗空位</span>
                <Button
                  size="mini"
                  color="primary"
                  disabled={mutationsDisabled}
                  data-testid={`add-slot-${g.post}-${squad ?? "none"}-${i}`}
                  onClick={() => onProxy(g.post, squad)}
                >
                  添加
                </Button>
              </>
            )}
          </div>
        );
      })}
      {g.extras.map((p) => (
        <div key={p.empId} style={{ marginLeft: 16, padding: "4px 0", fontSize: "1rem" }}>
          <RosterPerson p={p} onPick={onPick} onBind={onBind} />
        </div>
      ))}
    </div>
  );
}
