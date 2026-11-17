import React, { useCallback, useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";
import { Button, ConfigProvider, SpinLoading, Toast } from "antd-mobile";
import zhCN from "antd-mobile/es/locales/zh-CN";
import { HashRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { getMe, setRequestActivityHandler } from "@fdc/api-client";
import { dingtalkLogin, inDingTalk } from "./dingtalk";
import { AuthProvider } from "./auth";
import type { Me } from "@fdc/api-client";

// 全局接口 loading：有请求在飞时显示「加载中…」，延迟 200ms 避免快请求闪烁；不阻塞点击。
let _loadingTimer: ReturnType<typeof setTimeout> | undefined;
setRequestActivityHandler((active) => {
  if (active) {
    _loadingTimer = setTimeout(() => {
      Toast.show({ icon: "loading", content: "加载中…", duration: 0, maskClickable: true });
    }, 200);
  } else {
    if (_loadingTimer) clearTimeout(_loadingTimer);
    Toast.clear();
  }
});
import ScanPage from "./pages/Scan";
import ScanSimPage from "./pages/ScanSim";
import ConfirmPage from "./pages/confirm/ConfirmPage";
import ChiefReviewPage from "./pages/ChiefReview";
import CalendarPage from "./pages/Calendar";
import HomePage from "./pages/Home";
import SettingsPage from "./pages/Settings";
import DebugPage from "./pages/Debug";
import "antd-mobile/es/global";
import "./global.css";

type State = "loading" | "authed" | "needdd" | "dderror" | "debug";
type RetryMode = "login" | "session";
const IDENTITY_REFRESH_INTERVAL_MS = 60_000;

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function SessionIdentityRefresh({
  refreshMe,
  onSessionMissing,
}: {
  refreshMe: () => Promise<Me>;
  onSessionMissing: () => void;
}) {
  const location = useLocation();
  const lastRefreshAt = useRef(Date.now());
  const onSessionMissingRef = useRef(onSessionMissing);

  useEffect(() => {
    onSessionMissingRef.current = onSessionMissing;
  }, [onSessionMissing]);

  const refreshIfStale = useCallback(() => {
    const now = Date.now();
    if (now - lastRefreshAt.current < IDENTITY_REFRESH_INTERVAL_MS) return;
    lastRefreshAt.current = now;
    void refreshMe()
      .then((profile) => {
        if (!profile.userid) onSessionMissingRef.current();
      })
      .catch(() => undefined);
  }, [refreshMe]);

  useEffect(() => {
    refreshIfStale();
  }, [location.pathname, refreshIfStale]);

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") refreshIfStale();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [refreshIfStale]);

  return null;
}

function LoginGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<State>("loading");
  const [me, setMe] = useState<Me | null>(null);
  const [ddId, setDdId] = useState("");
  const [ddMsg, setDdMsg] = useState("");
  const [retryMode, setRetryMode] = useState<RetryMode>("login");
  const tapRef = useRef<{ n: number; t: number }>({ n: 0, t: 0 });
  const checkStarted = useRef(false);

  const refreshMe = useCallback(async () => {
    const next = await getMe();
    setMe(next);
    return next;
  }, []);

  const startLogin = async () => {
    if (!inDingTalk()) {
      setState("needdd");
      return;
    }
    let info: Awaited<ReturnType<typeof dingtalkLogin>>;
    try {
      info = await dingtalkLogin();
    } catch (error) {
      setDdMsg(errorMessage(error, "钉钉登录失败，请重试"));
      setDdId("");
      setRetryMode("login");
      setState("dderror");
      return;
    }
    if (info.matched) {
      try {
        const profile = await refreshMe();
        if (!profile.userid) throw new Error("登录成功但未建立会话");
        if (info.imported) Toast.show({ icon: "success", content: "已导入并登录" });
        setState("authed");
      } catch (error) {
        setDdId("");
        setDdMsg(`钉钉登录已成功，但获取用户信息失败：${errorMessage(error, "请重试")}`);
        setRetryMode("session");
        setState("dderror");
      }
      return;
    }
    // 后端已在同一次 authCode 流程中自动尝试首次导入；失败时直接展示原因。
    setDdId(info.ddUserid || info.userid || "");
    setDdMsg(info.message || "员工身份不匹配，请联系当前应用的 HR");
    setRetryMode("login");
    setState("dderror");
  };

  const retrySession = async () => {
    try {
      const profile = await refreshMe();
      if (profile.userid) {
        setState("authed");
        return;
      }
      await startLogin();
    } catch (error) {
      setDdMsg(`获取用户信息失败：${errorMessage(error, "请重试")}`);
      setRetryMode("session");
      setState("dderror");
    }
  };

  // 红字连点 12 次（1.5s 内）进入钉钉免登调试页，查看更多信息
  const onMsgTap = () => {
    const now = Date.now();
    const prev = tapRef.current;
    const n = now - prev.t < 1500 ? prev.n + 1 : 1;
    tapRef.current = { n, t: now };
    if (n >= 12) {
      tapRef.current = { n: 0, t: 0 };
      setState("debug");
    }
  };

  const check = () =>
    refreshMe()
      .then((m) => (m.userid ? setState("authed") : startLogin()))
      .catch(() => startLogin());

  useEffect(() => {
    if (checkStarted.current) return;
    checkStarted.current = true;
    void check();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (state === "loading")
    return (
      <div
        style={{
          minHeight: "100vh", display: "flex", flexDirection: "column",
          alignItems: "center", justifyContent: "center", gap: 12,
          background: "#f5f5f5", color: "#999",
        }}
      >
        <SpinLoading style={{ "--size": "36px" }} />
        <span style={{ fontSize: 14 }}>登录中…</span>
      </div>
    );
  if (state === "authed" && me) {
    return (
      <AuthProvider me={me} refreshMe={refreshMe}>
        <SessionIdentityRefresh
          refreshMe={refreshMe}
          onSessionMissing={() => {
            setState("loading");
            void startLogin();
          }}
        />
        {children}
      </AuthProvider>
    );
  }
  if (state === "debug") return <DebugPage />;

  if (state === "needdd") {
    // 隐藏入口：1.5s 内连点标题 12 次打开超级管理员登录页（同源 admin #/super-login）
    const onTitleTap = () => {
      const now = Date.now();
      const prev = tapRef.current;
      const n = now - prev.t < 1500 ? prev.n + 1 : 1;
      tapRef.current = { n, t: now };
      if (n >= 12) {
        tapRef.current = { n: 0, t: 0 };
        // 跳转电脑端（admin）的超级管理员登录页：生产同源 / ，开发环境为 5173 端口
        const base = import.meta.env.DEV
          ? `${window.location.protocol}//${window.location.hostname}:5173/`
          : "/";
        window.location.href = `${base}#/super-login`;
      }
    };
    return (
      <div style={{ padding: 24, minHeight: "100vh", background: "#f5f5f5" }}>
        <div style={{ background: "#fff", borderRadius: 8, padding: 20, marginTop: 40 }}>
          <div
            style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}
            data-testid="needdd-title"
            onClick={onTitleTap}
          >
            请在钉钉中打开
          </div>
          <div style={{ color: "#888", fontSize: 13, lineHeight: 1.8 }}>
            本系统仅支持钉钉登录。请通过<strong>钉钉工作台</strong>找到本应用入口打开使用。
          </div>
        </div>
      </div>
    );
  }

  // 钉钉身份未匹配：显示钉钉 id 供 HR 录入
  return (
    <div style={{ padding: 24, minHeight: "100vh", background: "#f5f5f5" }}>
      <div style={{ background: "#fff", borderRadius: 8, padding: 20, marginTop: 40 }}>
        <div
          style={{ fontSize: 16, fontWeight: 600, color: "#cf1322", marginBottom: 10 }}
          data-testid="dderror-msg"
          onClick={onMsgTap}
        >
          {ddMsg}
        </div>
        {ddId && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 12, color: "#999" }}>钉钉用户ID：</div>
            <div style={{ fontSize: 16, fontWeight: 700, wordBreak: "break-all", userSelect: "all" }}>{ddId}</div>
          </div>
        )}
        {ddId && (
          <div style={{ fontSize: 12, color: "#999", lineHeight: 1.7, marginBottom: 8 }}>
            系统已在本次登录中自动尝试导入身份。可重新登录再试；多次失败请联系 HR。
          </div>
        )}
        <Button
          fill="outline"
          block
          onClick={() => {
            setState("loading");
            void (retryMode === "session" ? retrySession() : startLogin());
          }}
        >
          {retryMode === "session" ? "重试获取用户信息" : "重新登录"}
        </Button>
      </div>
    </div>
  );
}

// antd-mobile DatePicker 默认 enUS（月份显示 May 等）→ 全局配 zh-CN，月份/年显示为「5月」「2026年」
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN}>
      <HashRouter>
        <LoginGate>
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/scan-sim" element={<ScanSimPage />} />
            <Route path="/scan" element={<ScanPage />} />
            <Route path="/confirm" element={<ConfirmPage />} />
            <Route path="/chief-review" element={<ChiefReviewPage />} />
            <Route path="/calendar" element={<CalendarPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/debug" element={<DebugPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </LoginGate>
      </HashRouter>
    </ConfigProvider>
  </React.StrictMode>
);
