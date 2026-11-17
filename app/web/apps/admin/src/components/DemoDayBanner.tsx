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
        background: "rgba(255,255,255,0.15)",
        padding: "4px 12px",
        fontSize: 12,
        borderRadius: 4,
      }}
    >
      演示数据日：{ctx.today}（T-2={ctx.t2}）
    </div>
  );
}
