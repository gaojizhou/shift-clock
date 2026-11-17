import { useAuth } from "../auth";

/**
 * 当前员工信息条（姓名 + 工号），扫码上岗 / 模拟扫码 共用，保持一致。
 * 传入 name/userid 则直接用；否则复用登录门禁已加载的 /me。
 */
export default function EmployeeBadge({
  name,
  userid,
  style,
}: {
  name?: string;
  userid?: string;
  style?: React.CSSProperties;
}) {
  const { me } = useAuth();
  const displayName = name ?? me.name;
  const displayUserid = userid ?? me.userid;

  return (
    <div
      data-testid="employee-badge"
      style={{ display: "flex", alignItems: "baseline", gap: 8, ...style }}
    >
      <span style={{ fontSize: 16, fontWeight: 600 }}>{displayName || "（未获取姓名）"}</span>
      <span style={{ fontSize: 12, color: "#888" }}>工号：{displayUserid || "-"}</span>
    </div>
  );
}
