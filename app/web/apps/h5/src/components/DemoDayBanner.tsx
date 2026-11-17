import { useEffect, useState } from "react";
import { getDemoContext, type DemoContext } from "@fdc/api-client";

export default function DemoDayBanner() {
  const [ctx, setCtx] = useState<DemoContext | null>(null);

  useEffect(() => {
    getDemoContext().then(setCtx).catch(() => setCtx(null));
  }, []);

  if (!ctx) return null;

  return (
    <div
      data-testid="demo-day-banner"
      style={{
        background: "#fff7e6",
        color: "#ad6800",
        padding: "6px 12px",
        fontSize: 12,
        borderBottom: "1px solid #ffd591",
      }}
    >
      演示数据日：{ctx.today}（T-2={ctx.t2}）
    </div>
  );
}
