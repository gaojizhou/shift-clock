import { useEffect, useState } from "react";
import { List, NavBar, Switch, Toast, SpinLoading } from "antd-mobile";
import { useNavigate } from "react-router-dom";
import { getMyNotifySetting, setMyNotifySetting } from "@fdc/api-client";

export default function SettingsPage() {
  const nav = useNavigate();
  const [loading, setLoading] = useState(true);
  const [pushEnabled, setPushEnabled] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getMyNotifySetting()
      .then((r) => setPushEnabled(!!r.pushEnabled))
      .catch(() => Toast.show({ content: "加载设置失败" }))
      .finally(() => setLoading(false));
  }, []);

  const onToggle = async (v: boolean) => {
    setSaving(true);
    try {
      const r = await setMyNotifySetting(v);
      setPushEnabled(!!r.pushEnabled);
      Toast.show({ icon: "success", content: v ? "已开启钉钉通知" : "已关闭钉钉通知" });
    } catch (e) {
      const detail = (e as { data?: { detail?: string } })?.data?.detail;
      Toast.show({ content: detail || "设置失败，请重试" });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ background: "#f5f5f5", minHeight: "100vh" }}>
      <NavBar onBack={() => nav("/")} style={{ background: "#1677FF", color: "#fff" }}>
        设置
      </NavBar>
      {loading ? (
        <div style={{ textAlign: "center", padding: "56px 0" }}>
          <SpinLoading style={{ "--size": "32px" }} />
        </div>
      ) : (
        <List header="消息通知">
          <List.Item
            extra={
              <Switch
                checked={pushEnabled}
                loading={saving}
                onChange={(v) => void onToggle(v)}
                data-testid="setting-push-enabled"
              />
            }
            description="关闭后将不再收到任何钉钉工作通知（轮班提醒、异常待确认、缺编提醒等）。"
          >
            钉钉通知
          </List.Item>
        </List>
      )}
    </div>
  );
}
