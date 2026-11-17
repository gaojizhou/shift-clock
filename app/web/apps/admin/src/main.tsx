import React, { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ReactDOM from "react-dom/client";
import { Alert, Button, ConfigProvider, Input, Select, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import { HashRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

// antd 的日期组件文本（月份/星期）由 dayjs 渲染：仅配 ConfigProvider locale 不够，
// 还须把 dayjs 全局 locale 设为 zh-cn，否则日历头仍显示英文月份(May/Jun)。
dayjs.locale("zh-cn");
import {
  adminLogin,
  confirmMyRegion,
  getMe,
  getRegions,
  logout,
  setPageRegionScope,
} from "@fdc/api-client";
import FurnacesPage from "./pages/Furnaces";
import EmployeesPage from "./pages/Employees";
import DictionariesPage from "./pages/Dictionaries";
import AnomaliesPage from "./pages/Anomalies";
import AuditLogsPage from "./pages/AuditLogs";
import ConnectionPoolPage from "./pages/ConnectionPool";
import DingtalkLoadTestPage from "./pages/DingtalkLoadTest";
import EscalationsPage from "./pages/Escalations";
import ProxyQuotaPage from "./pages/ProxyQuota";
import ExportPage from "./pages/Export";
import ShiftPushPage from "./pages/ShiftPush";
import RawAttendanceSyncPage from "./pages/RawAttendanceSync";
import EmployeeSyncPage from "./pages/EmployeeSync";
import {
  CACHEABLE_ADMIN_PATHS,
  CachedAdminNavLink,
  CachedNavLink,
  NavigationTestEnvironmentContext,
  PageCacheProvider,
  PageCacheStamp,
  SUPPORTED_ADMIN_PATHS,
  installAdminPageResponseCache,
  setCacheIdentity,
  usePageCache,
} from "./pageCache";
import "antd/dist/reset.css";

installAdminPageResponseCache();

const AuthRefreshContext = createContext<() => void>(() => undefined);

function defaultPathForRole(role: string): string {
  if (role === "stat" || role === "chief") return "/anomalies";
  if (role === "hr" || role === "admin") return "/furnaces";
  return "/forbidden";
}

function LoginScreen({ onLogin }: { onLogin: (to?: string) => void }) {
  // 管理后台自身不做钉钉免登（与 H5 同源共享会话）。无会话时引导到 H5 首页完成钉钉登录，
  // 登录成功后由 H5 首页「进入管理后台」回到本页。
  void onLogin;
  const goH5Login = () => {
    window.location.href = import.meta.env.DEV
      ? `${window.location.protocol}//${window.location.hostname}:5174/`
      : "/h5/";
  };
  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#f0f2f5" }}>
      <div style={{ width: 360, padding: 28, background: "#fff", borderRadius: 10, boxShadow: "0 2px 12px rgba(0,0,0,.08)" }}>
        <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 8 }}>三班倒打卡 · 管理后台</div>
        <div style={{ color: "#666", fontSize: 13, lineHeight: 1.8, marginBottom: 16 }}>
          管理后台需<strong>钉钉登录</strong>。请点下方「前往钉钉登录」到首页完成登录，登录后从首页「进入管理后台」返回。
        </div>
        <Button type="primary" block onClick={goH5Login}>
          前往钉钉登录
        </Button>
      </div>
    </div>
  );
}

/** 超级管理员独立入口（#/super-login）：TOTP 两步验证动态码。
 *  支持 `#/super-login?code=NNNNNN` 携带动态码自动登录（测试脚本一键打开用）。 */
function SuperLoginPage() {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const loc = useLocation();

  const doLogin = async (override?: string) => {
    const c = (override ?? code).trim();
    if (!c) return;
    setBusy(true);
    try {
      await adminLogin(c);
      window.location.hash = "#/shift-push";
      window.location.reload();
    } catch (e) {
      const status = (e as { status?: number }).status;
      message.error(status === 403 ? "超级管理员入口未开启" : "动态码不正确或已过期");
    } finally {
      setBusy(false);
    }
  };

  // URL 携带 ?code=6位 时自动填入并提交（动态码有时效，过期则停在本页提示重输）。
  // HashRouter 下查询串在 hash 内（#/super-login?code=123456），location.search 取不到，
  // 故直接从 hash 解析，loc.search 兜底。
  useEffect(() => {
    const hash = window.location.hash;
    const qs = hash.includes("?") ? hash.slice(hash.indexOf("?")) : loc.search;
    const c = new URLSearchParams(qs).get("code");
    if (c && /^\d{6}$/.test(c)) {
      setCode(c);
      void doLogin(c);
    }
    // 仅首次进入时尝试，code 变化由用户手输处理
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#141414" }}>
      <div style={{ width: 360, padding: 28, background: "#fff", borderRadius: 10 }}>
        <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 8 }}></div>
        <div style={{ color: "#888", fontSize: 13, lineHeight: 1.8, marginBottom: 16 }}></div>
        <Input
          data-testid="super-totp"
          size="large"
          maxLength={6}
          inputMode="numeric"
          autoFocus
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          onPressEnter={() => doLogin()}
          placeholder="000000"
          style={{ textAlign: "center", letterSpacing: 8, fontSize: 22 }}
        />
        <Button
          data-testid="super-login"
          block
          type="primary"
          size="large"
          loading={busy}
          disabled={code.length !== 6}
          style={{ marginTop: 14 }}
          onClick={() => doLogin()}
        >
          验证
        </Button>
        <Button type="link" block style={{ marginTop: 8 }} onClick={() => { window.location.hash = "#/"; }}>
          返回普通钉钉登录
        </Button>
      </div>
    </div>
  );
}

function RequireRole({
  roles,
  children,
  active = true,
}: {
  roles: string[];
  children: React.ReactNode;
  /** 该 section 是否为当前可见路径。缓存页会常驻挂载（非当前路径用 display:none 隐藏），
   *  隐藏 section 里的 RequireRole 若渲染 Navigate 会把用户弹到 /forbidden；此时应返回 null。 */
  active?: boolean;
}) {
  const refreshAuth = useContext(AuthRefreshContext);
  const [role, setRole] = useState("");
  const [authError, setAuthError] = useState<{ status?: number; message: string } | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);

  useEffect(() => {
    if (!active) return;
    let current = true;
    setAuthError(null);
    getMe()
      .then((m) => {
        if (!current) return;
        setRole(m.role);
        setAuthError(null);
      })
      .catch((error: unknown) => {
        if (!current) return;
        const value = error as { status?: number; message?: string };
        if (value.status === 401 || value.status === 403) {
          setRole("");
          setAuthError(null);
          refreshAuth();
          return;
        }
        setAuthError({ status: value.status, message: value.message || "身份信息加载失败" });
      });
    return () => { current = false; };
  }, [active, refreshAuth, retrySeq]);

  if (authError) {
    if (!active) return role ? <>{children}</> : null;
    const banner = (
      <Alert
        type="error"
        showIcon
        message="身份信息暂时无法加载"
        description={authError.message}
        action={<Button onClick={() => setRetrySeq((value) => value + 1)}>重试</Button>}
      />
    );
    if (role) {
      return (
        <>
          {banner}
          {children}
        </>
      );
    }
    return banner;
  }
  if (!role) return null;
  if (role !== "admin" && !roles.includes(role)) {
    return active ? <Navigate to="/forbidden" replace state={{ roles }} /> : null;
  }
  // 已授权的隐藏页继续渲染，由外层 section 隐藏，从而保留页面本地状态。
  return <>{children}</>;
}

const ROLE_LABEL: Record<string, string> = {
  worker: "操作工",
  chief: "值班长",
  hr: "人事专员",
  stat: "统计员",
  admin: "系统管理员",
  repairer: "维修工",
  leader: "班组长",
};

function ForbiddenPage() {
  const loc = useLocation();
  const roles = (loc.state as { roles?: string[] } | null)?.roles ?? [];
  const allowed = roles.length
    ? [...roles.map((r) => ROLE_LABEL[r] ?? r), ROLE_LABEL.admin].join(" / ")
    : ROLE_LABEL.admin;
  return (
    <div style={{ padding: 24 }}>
      <h2>无权限访问</h2>
      <p>当前角色无权访问该管理功能，请切换为{allowed}账号。</p>
    </div>
  );
}

function RegionSelectionScreen({
  onConfirmed,
  onLogout,
}: {
  onConfirmed: () => void;
  onLogout: () => void;
}) {
  const [regions, setRegions] = useState<{ value: string; label: string }[]>([]);
  const [region, setRegion] = useState<string>();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getRegions()
      .then((r) => setRegions(r.items.map((item) => ({ value: item.name, label: item.name }))))
      .catch(() => message.error("地区列表加载失败"));
  }, []);

  const submit = async () => {
    if (!region) return;
    setSaving(true);
    try {
      await confirmMyRegion(region);
      message.success("所在地区已确认");
      onConfirmed();
    } catch (e) {
      const detail = (e as { data?: { detail?: string } }).data?.detail;
      message.error(detail || "所在地区确认失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#f0f2f5" }}>
      <div style={{ width: 400, padding: 28, background: "#fff", borderRadius: 10, boxShadow: "0 2px 12px rgba(0,0,0,.08)" }}>
        <h2 style={{ marginTop: 0 }}>选择所在地区</h2>
        <p style={{ color: "#666", lineHeight: 1.7 }}>
          当前账号尚未配置地区。确认后页面将按该地区显示；后续如需修改，请联系管理员。
        </p>
        <Select
          data-testid="region-selection"
          style={{ width: "100%", marginBottom: 16 }}
          placeholder="请选择一个地区"
          value={region}
          onChange={setRegion}
          options={regions}
        />
        <Button type="primary" block loading={saving} disabled={!region} onClick={submit}>
          确认地区
        </Button>
        <Button type="link" block onClick={onLogout}>退出登录</Button>
      </div>
    </div>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  const [role, setRole] = useState("");
  const [me, setMe] = useState<
    {
      userid: string;
      name?: string;
      super?: boolean;
      impersonating?: boolean;
      region?: string;
      requiresRegionSelection?: boolean;
      adminTestBanner?: boolean;
    } | null
  >(null);
  const [ready, setReady] = useState(false);
  const loc = useLocation();
  const { clearAll } = usePageCache();

  const refresh = useCallback(() =>
    getMe()
      .then((m) => {
        if (m.userid) {
          const scoped = (m.role === "hr" || m.role === "stat") ? m.region : undefined;
          setPageRegionScope(scoped);
          // 页面响应缓存按身份分桶：会话过期换账号后不会读到上一身份的缓存。
          setCacheIdentity(`${m.userid}:${m.role}:${m.region || ""}`);
          setRole(m.role);
          setMe({
            userid: m.userid,
            name: m.name,
            super: m.super,
            impersonating: m.impersonating,
            region: m.region,
            requiresRegionSelection: m.requiresRegionSelection,
            adminTestBanner: m.adminTestBanner === true,
          });
        } else {
          setRole("");
          setMe(null);
        }
      })
      .catch(() => {
        setRole("");
        setMe(null);
        setPageRegionScope(undefined);
        setCacheIdentity("");
        clearAll();
      })
      .finally(() => setReady(true)), [clearAll]);
  const requestAuthRefresh = useCallback(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const doLogout = async () => {
    await logout().catch(() => undefined);
    setMe(null);
    setRole("");
    setPageRegionScope(undefined);
    clearAll();
    window.location.hash = "#/";
  };

  // 超级管理员独立入口页：无会话也可访问，不渲染导航
  if (loc.pathname === "/super-login") return <>{children}</>;

  if (!ready) return null;
  if (!me)
    return (
      <LoginScreen
        onLogin={(to) => {
          setReady(false);
          refresh();
          window.location.hash = to ?? "#/";
        }}
      />
    );

  if ((role === "hr" || role === "stat") && me.requiresRegionSelection) {
    return <RegionSelectionScreen onConfirmed={refresh} onLogout={doLogout} />;
  }

  const testEnvironment = me.adminTestBanner === true;
  const isAdmin = me.super || role === "admin";
  const isHrAdmin = role === "hr" || role === "admin";
  const showFurnaces = isHrAdmin;  // 工作点编制仅人事/管理员；统计员无权查看
  const showAnomalies = role === "stat" || role === "chief" || isHrAdmin;
  const showProxy = role === "stat" || isHrAdmin;
  const showExport = role === "stat" || isHrAdmin;

  // 「管理」小导航条：仅超级管理员会话（TOTP 进入）且当前身份为管理员可见
  // （超管切换身份模拟非管理员角色时不显示）
  const adminBarItems: { to: string; label: string }[] = me.super && role === "admin"
    ? [
      { to: "/dictionaries", label: "基础字典" },
      { to: "/employees", label: "员工管理" },
      { to: "/audit-logs", label: "操作日志" },
      { to: "/shift-push", label: "推送管理" },
    ]
    : [];

  return (
    <AuthRefreshContext.Provider value={requestAuthRefresh}>
    <NavigationTestEnvironmentContext.Provider value={testEnvironment}>
    <div style={{ minHeight: "100vh" }}>
      <header
        data-testid="admin-navigation"
        style={{
          background: testEnvironment ? "#fadb14" : "#1677FF",
          color: testEnvironment ? "#3d3000" : "#fff",
          padding: "12px 24px",
          display: "flex",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <strong>三班倒打卡</strong>
        {testEnvironment && (
          <strong data-testid="test-environment-badge" style={{
            padding: "2px 8px", border: "1px solid #614700", borderRadius: 4,
            background: "#fffbe6", color: "#614700", whiteSpace: "nowrap",
          }}>测试环境</strong>
        )}
        {showFurnaces && <CachedNavLink to="/furnaces">工作点编制</CachedNavLink>}
        {showAnomalies && <CachedNavLink to="/anomalies">劳勤工作台</CachedNavLink>}
        {role === "admin" && <CachedNavLink to="/escalations">升级记录</CachedNavLink>}
        {showProxy && <CachedNavLink to="/proxy-quota">代签额度</CachedNavLink>}
        {showExport && <CachedNavLink to="/export">月度导出</CachedNavLink>}
        {isHrAdmin && <CachedNavLink to="/raw-attendance-sync">原始考勤同步</CachedNavLink>}
        {isAdmin && <CachedNavLink to="/employee-sync">通讯录同步</CachedNavLink>}
        {/* 非超管会话的管理入口放在主导航（「管理」小条仅超管可见） */}
        {!me.super && role === "admin" && (
          <>
            <CachedNavLink to="/dictionaries">基础字典</CachedNavLink>
            <CachedNavLink to="/employees">员工管理</CachedNavLink>
            <CachedNavLink to="/audit-logs">操作日志</CachedNavLink>
            <CachedNavLink to="/shift-push">推送管理</CachedNavLink>
          </>
        )}
        {!me.super && (role === "hr" || role === "stat") && (
          <>
            <CachedNavLink to="/employees">员工查询</CachedNavLink>
            <CachedNavLink to="/audit-logs">操作日志</CachedNavLink>
          </>
        )}
        <span style={{ marginLeft: "auto", fontSize: 13 }}>
          {me.name ?? me.userid}（{role}）
          {(role === "hr" || role === "stat") && (
            <> · 地区：{me.region || "未配置"}</>
          )}
        </span>
        <Button size="small" onClick={doLogout}>
          退出
        </Button>
      </header>
      {adminBarItems.length > 0 && (
        <nav
          data-testid="admin-bar"
          style={{
            background: testEnvironment ? "#fffbe6" : "#f0f5ff",
            borderBottom: testEnvironment ? "1px solid #d4b106" : "1px solid #d6e4ff",
            padding: "8px 24px",
            display: "flex",
            gap: 8,
            alignItems: "center",
            fontSize: 13,
          }}
        >
          <span style={{ color: testEnvironment ? "#614700" : "#2f54eb", fontWeight: 600 }}>超级管理员导航条</span>
          {adminBarItems.map((it) => (
            <CachedAdminNavLink key={it.to} to={it.to}>{it.label}</CachedAdminNavLink>
          ))}
        </nav>
      )}
      <main style={{ padding: 24 }}>{children}</main>
    </div>
    </NavigationTestEnvironmentContext.Provider>
    </AuthRefreshContext.Provider>
  );
}

function HomeRedirect() {
  const [role, setRole] = useState("");
  const refreshAuth = useContext(AuthRefreshContext);
  useEffect(() => {
    let current = true;
    getMe()
      .then((m) => { if (current) setRole(m.role); })
      .catch(() => { if (current) refreshAuth(); });
    return () => { current = false; };
  }, [refreshAuth]);
  if (!role) return null;
  return <Navigate to={defaultPathForRole(role)} replace />;
}

const CACHED_ROUTE_ELEMENTS: Record<string, (active: boolean) => React.ReactNode> = {
  "/furnaces": (active) => <RequireRole roles={["hr"]} active={active}><FurnacesPage active={active} /></RequireRole>,
  "/employees": (active) => <RequireRole roles={["hr", "stat"]} active={active}><EmployeesPage active={active} /></RequireRole>,
  "/dictionaries": (active) => <RequireRole roles={[]} active={active}><DictionariesPage active={active} /></RequireRole>,
  "/anomalies": (active) => <RequireRole roles={["stat", "hr", "chief"]} active={active}><AnomaliesPage active={active} /></RequireRole>,
  "/audit-logs": (active) => <RequireRole roles={["stat", "hr"]} active={active}><AuditLogsPage active={active} /></RequireRole>,
  "/shift-push": (active) => <RequireRole roles={[]} active={active}><ShiftPushPage active={active} /></RequireRole>,
  "/proxy-quota": (active) => <RequireRole roles={["stat", "hr"]} active={active}><ProxyQuotaPage active={active} /></RequireRole>,
  "/escalations": (active) => <RequireRole roles={[]} active={active}><EscalationsPage active={active} /></RequireRole>,
  "/export": (active) => <RequireRole roles={["stat", "hr"]} active={active}><ExportPage active={active} /></RequireRole>,
  "/raw-attendance-sync": (active) => <RequireRole roles={["hr"]} active={active}><RawAttendanceSyncPage active={active} /></RequireRole>,
  "/employee-sync": (active) => <RequireRole roles={[]} active={active}><EmployeeSyncPage /></RequireRole>,
  "/connection-pool": (active) => <RequireRole roles={[]} active={active}><ConnectionPoolPage /></RequireRole>,
  "/dingtalk-load-test": (active) => <RequireRole roles={[]} active={active}><DingtalkLoadTestPage /></RequireRole>,
};

function CachedPageSection({
  active,
  children,
}: {
  active: boolean;
  children: React.ReactNode;
}) {
  const containerRef = useRef<HTMLElement>(null);
  return (
    <section
      ref={containerRef}
      style={{
        display: active ? "block" : "none",
        pointerEvents: active ? undefined : "none",
        position: "relative",
      }}
      aria-hidden={active ? undefined : true}
    >
      {/* Dropdown/Popconfirm 等弹层挂到当前缓存 section，失活时随 section 一起隐藏。
          Modal/Drawer 由各页面的 active effect 主动关闭。 */}
      <ConfigProvider getPopupContainer={() => containerRef.current ?? document.body}>
        {children}
      </ConfigProvider>
    </section>
  );
}

function CachedAdminRoutes() {
  const loc = useLocation();
  const { entries, activate } = usePageCache();
  const currentIsCacheable = CACHEABLE_ADMIN_PATHS.has(loc.pathname);
  const [mountedPaths, setMountedPaths] = useState<string[]>(() =>
    currentIsCacheable ? [loc.pathname] : []
  );

  useLayoutEffect(() => {
    if (!currentIsCacheable) return;
    activate(loc.pathname);
    setMountedPaths((current) => current.includes(loc.pathname)
      ? current
      : [...current, loc.pathname]);
  }, [activate, currentIsCacheable, loc.pathname]);

  useEffect(() => {
    const cachedPaths = new Set(entries.map((entry) => entry.path));
    setMountedPaths((current) => current.filter((path) => cachedPaths.has(path) || path === loc.pathname));
  }, [entries, loc.pathname]);

  // 仅显式清除缓存时递增 generation，普通切页和时间流逝不重建页面。
  const visibleEntries = useMemo(() => {
    const byPath = new Map(entries.map((entry) => [entry.path, entry]));
    if (currentIsCacheable && !byPath.has(loc.pathname)) {
      byPath.set(loc.pathname, { path: loc.pathname, cachedAt: Date.now(), generation: 0 });
    }
    const paths = currentIsCacheable && !mountedPaths.includes(loc.pathname)
      ? [...mountedPaths, loc.pathname]
      : mountedPaths;
    return paths
      .map((path) => byPath.get(path))
      .filter(Boolean) as typeof entries;
  }, [currentIsCacheable, entries, loc.pathname, mountedPaths]);

  const cachedSections = visibleEntries.map((entry) => {
    const active = entry.path === loc.pathname;
    return (
      <CachedPageSection
        key={`${entry.path}:${entry.generation}`}
        active={active}
      >
        <PageCacheStamp cachedAt={entry.cachedAt} />
        {CACHED_ROUTE_ELEMENTS[entry.path](active)}
      </CachedPageSection>
    );
  });

  return <>
    {cachedSections}
    {!currentIsCacheable && (
      <Routes>
        <Route path="/" element={<HomeRedirect />} />
        <Route path="/super-login" element={<SuperLoginPage />} />
        <Route path="/forbidden" element={<ForbiddenPage />} />
        <Route path="/debug-push" element={<Navigate to="/shift-push" replace />} />
        {[...SUPPORTED_ADMIN_PATHS]
          .filter((path) => !CACHEABLE_ADMIN_PATHS.has(path))
          .map((path) => (
            <Route key={path} path={path} element={CACHED_ROUTE_ELEMENTS[path](true)} />
          ))}
        <Route path="*" element={<Navigate to="/forbidden" replace />} />
      </Routes>
    )}
  </>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: "#1677FF", borderRadius: 8 } }}>
      <HashRouter>
        <PageCacheProvider>
          <Layout><CachedAdminRoutes /></Layout>
        </PageCacheProvider>
      </HashRouter>
    </ConfigProvider>
  </React.StrictMode>
);
