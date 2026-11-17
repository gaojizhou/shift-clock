import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  CACHEABLE_ADMIN_PATHS,
  cacheOwnerPath,
  isAuthProbeRequest,
  isBackgroundStatusRequest,
  isLiveDataRequest,
} from "./pageCachePolicy";

export { CACHEABLE_ADMIN_PATHS, SUPPORTED_ADMIN_PATHS } from "./pageCachePolicy";

export type PageCacheEntry = {
  path: string;
  cachedAt: number;
  generation: number;
};

const PAGE_CACHE_ENTRIES_KEY = "fdc-admin-page-cache-entries-v1";
const PAGE_DATA_KEY_PREFIX = "fdc-admin-page-data-v1";
const PAGE_DATA_SIZE_INDEX_PREFIX = "fdc-admin-page-data-sizes-v1";

// 当前登录身份（userid:role:region）。getMe 是异步的，安装拦截器时先为空，
// 待 getMe 返回后由 setCacheIdentity 补上。所有缓存键都拼上该身份，避免换账号读到旧身份缓存。
let cacheIdentity = "";
const entriesKey = () => `${PAGE_CACHE_ENTRIES_KEY}:${cacheIdentity}`;
const pageDataKey = (path: string) => `${PAGE_DATA_KEY_PREFIX}:${cacheIdentity}:${path}`;
const pageDataSizeIndexKey = () => `${PAGE_DATA_SIZE_INDEX_PREFIX}:${cacheIdentity}`;

// 每页缓存上限：条目数 / 单条响应体积 / 总体积。超出按最旧 cachedAt 淘汰（LRU）。
const MAX_RESPONSES_PER_PAGE = 50;
const MAX_RESPONSE_BODY_BYTES = 200 * 1024; // 200KB
const MAX_TOTAL_PAGE_DATA_BYTES = 3 * 1024 * 1024; // 本标签页全部页面共享 3MB
const MAX_PAGE_DATA_BYTES = Math.floor(
  MAX_TOTAL_PAGE_DATA_BYTES / CACHEABLE_ADMIN_PATHS.size,
); // 按可缓存页数分摊，避免逐页 1.5MB 累积击穿 origin 配额
const PAGE_CACHE_STATUS_EVENT = "fdc-page-cache-status";
const PAGE_CACHE_IDENTITY_EVENT = "fdc-page-cache-identity";
let cacheEpoch = 0;
const pathCacheEpoch = new Map<string, number>();
let pageCacheWriteDisabled = false;
let pageCacheWriteWarned = false;

const volatilePageCaches = new Map<string, Set<() => void>>();

/** 全局缓存纪元：每次写请求（或身份切换）清空会话缓存时自增。
 *  页面据此判断「上次刷新之后是否发生过写入」，决定重激活时是否需要回源，
 *  避免写后仍展示旧数据，也避免每次切回都重新拉取。 */
export function getPageCacheEpoch(): number {
  return cacheEpoch;
}

/** 注册模块级内存缓存的清理器，使页面/身份失效不会遗留 React 树外的数据。 */
export function registerVolatilePageCache(path: string, clear: () => void) {
  const clearers = volatilePageCaches.get(path) ?? new Set<() => void>();
  clearers.add(clear);
  volatilePageCaches.set(path, clearers);
  return () => clearers.delete(clear);
}

function resetPageCacheWriteStatus() {
  const changed = pageCacheWriteDisabled || pageCacheWriteWarned;
  pageCacheWriteDisabled = false;
  pageCacheWriteWarned = false;
  if (changed && typeof window !== "undefined") {
    window.dispatchEvent(new Event(PAGE_CACHE_STATUS_EVENT));
  }
}

function loadEntries(): PageCacheEntry[] {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(entriesKey()) || "[]");
    return Array.isArray(parsed)
      ? parsed.filter((entry) => CACHEABLE_ADMIN_PATHS.has(entry?.path))
      : [];
  } catch {
    return [];
  }
}

function clearPageSessionData(path: string) {
  pathCacheEpoch.set(path, (pathCacheEpoch.get(path) ?? 0) + 1);
  volatilePageCaches.get(path)?.forEach((clear) => clear());
  try {
    const storageKey = pageDataKey(path);
    sessionStorage.removeItem(storageKey);
    const index = loadPageSizeIndex(false);
    if (index && storageKey in index) {
      delete index[storageKey];
      savePageSizeIndex(index);
    }
  } catch { /* ignore */ }
}

// 清空所有页面响应缓存（含 entries 索引），跨身份都清。写请求后与 setCacheIdentity 换账号时复用。
function clearAllSessionData() {
  cacheEpoch += 1;
  volatilePageCaches.forEach((clearers) => clearers.forEach((clear) => clear()));
  try {
    const dataPrefix = `${PAGE_DATA_KEY_PREFIX}:`;
    for (const key of Object.keys(sessionStorage)) {
      if (
        key.startsWith(dataPrefix)
        || key.startsWith(`${PAGE_CACHE_ENTRIES_KEY}:`)
        || key.startsWith(`${PAGE_DATA_SIZE_INDEX_PREFIX}:`)
      ) {
        sessionStorage.removeItem(key);
      }
    }
  } catch { /* ignore */ }
  resetPageCacheWriteStatus();
}

type PageCacheValue = {
  entries: PageCacheEntry[];
  activate: (path: string) => void;
  invalidate: (path: string) => void;
  clear: (path: string, active: boolean) => void;
  clearAll: () => void;
};

const PageCacheContext = createContext<PageCacheValue | null>(null);

export function PageCacheProvider({ children }: { children: React.ReactNode }) {
  const [entries, setEntries] = useState<PageCacheEntry[]>(loadEntries);

  useEffect(() => {
    try { sessionStorage.setItem(entriesKey(), JSON.stringify(entries)); } catch { /* ignore */ }
  }, [entries]);

  useEffect(() => {
    const onIdentity = () => setEntries([]);
    window.addEventListener(PAGE_CACHE_IDENTITY_EVENT, onIdentity);
    return () => window.removeEventListener(PAGE_CACHE_IDENTITY_EVENT, onIdentity);
  }, []);

  const activate = useCallback((path: string) => {
    const now = Date.now();
    // 已缓存的页面不因时间流逝重建，保留组件状态和响应。
    setEntries((current) => current.some((entry) => entry.path === path)
      ? current
      : [...current, { path, cachedAt: now, generation: 0 }]);
  }, []);

  const clear = useCallback((path: string, active: boolean) => {
    clearPageSessionData(path);
    resetPageCacheWriteStatus();
    setEntries((current) => {
      if (!active) return current.filter((entry) => entry.path !== path);
      // 当前页清缓存后立即重新缓存，使页面组件以新 key 重建并重新拉取数据。
      return current.map((entry) => entry.path === path
        ? { ...entry, cachedAt: Date.now(), generation: entry.generation + 1 }
        : entry);
    });
  }, []);

  const invalidate = useCallback((path: string) => {
    clearPageSessionData(path);
    // 仅废弃接口响应并更新时间，不改变 generation，当前页面和筛选条件保持不变。
    setEntries((current) => current.map((entry) => entry.path === path
      ? { ...entry, cachedAt: Date.now() }
      : entry));
  }, []);

  const clearAll = useCallback(() => {
    clearAllSessionData();
    setEntries([]);
  }, []);
  const value = useMemo(
    () => ({ entries, activate, invalidate, clear, clearAll }),
    [entries, activate, invalidate, clear, clearAll],
  );
  return <PageCacheContext.Provider value={value}>{children}</PageCacheContext.Provider>;
}

type StoredResponse = {
  status: number;
  statusText: string;
  headers: [string, string][];
  body: string;
  cachedAt: number;
};

type StoredPageData = {
  responses: Record<string, StoredResponse>;
};

function currentAdminPath(): string {
  const hash = window.location.hash.replace(/^#/, "");
  return hash.split("?")[0] || "/";
}

function readPageData(path: string): StoredPageData {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(pageDataKey(path)) || "{}");
    const raw = parsed.responses && typeof parsed.responses === "object" ? parsed.responses : {};
    const responses: Record<string, StoredResponse> = {};
    for (const [key, value] of Object.entries(raw)) {
      const item = value as Partial<StoredResponse>;
      // 时间戳仅用于容量淘汰；有效响应不因存放时间而失效。
      if (typeof item.cachedAt === "number") {
        responses[key] = value as StoredResponse;
      }
    }
    return { responses };
  } catch {
    return { responses: {} };
  }
}

function serializedPageBytes(data: StoredPageData): number {
  return new TextEncoder().encode(JSON.stringify(data)).length;
}

type PageSizeIndex = Record<string, number>;

function savePageSizeIndex(index: PageSizeIndex) {
  sessionStorage.setItem(pageDataSizeIndexKey(), JSON.stringify(index));
}

function loadPageSizeIndex(rebuild = true): PageSizeIndex | null {
  try {
    const raw = sessionStorage.getItem(pageDataSizeIndexKey());
    if (raw !== null) {
      const parsed = JSON.parse(raw) as PageSizeIndex;
      return parsed && typeof parsed === "object" ? parsed : {};
    }
    if (!rebuild) return null;
    const prefix = `${PAGE_DATA_KEY_PREFIX}:${cacheIdentity}:`;
    const index: PageSizeIndex = {};
    for (const storageKey of Object.keys(sessionStorage)) {
      if (!storageKey.startsWith(prefix)) continue;
      const value = sessionStorage.getItem(storageKey);
      if (value !== null) index[storageKey] = new TextEncoder().encode(value).length;
    }
    savePageSizeIndex(index);
    return index;
  } catch {
    return rebuild ? {} : null;
  }
}

function trimGlobalPageData(targetBytes = MAX_TOTAL_PAGE_DATA_BYTES) {
  try {
    const index = loadPageSizeIndex() ?? {};
    let total = Object.values(index).reduce((sum, bytes) => sum + bytes, 0);
    // 常规写入只读取小型体积索引；未超预算时不解析任何页面缓存。
    if (total <= targetBytes) return;
    const pages = new Map<string, StoredPageData>();
    const refs: { storageKey: string; responseKey: string; cachedAt: number }[] = [];
    for (const storageKey of Object.keys(index)) {
      const parsed = JSON.parse(sessionStorage.getItem(storageKey) || "{}") as StoredPageData;
      if (!parsed.responses || typeof parsed.responses !== "object") {
        total -= index[storageKey] ?? 0;
        delete index[storageKey];
        continue;
      }
      pages.set(storageKey, parsed);
      for (const [responseKey, response] of Object.entries(parsed.responses)) {
        refs.push({ storageKey, responseKey, cachedAt: response.cachedAt ?? 0 });
      }
    }
    const changed = new Set<string>();
    for (const ref of refs.sort((a, b) => a.cachedAt - b.cachedAt)) {
      if (total <= targetBytes) break;
      const page = pages.get(ref.storageKey);
      if (!page?.responses[ref.responseKey]) continue;
      const before = serializedPageBytes(page);
      delete page.responses[ref.responseKey];
      total -= before - serializedPageBytes(page);
      changed.add(ref.storageKey);
    }
    changed.forEach((storageKey) => {
      const page = pages.get(storageKey);
      if (page) {
        const serialized = JSON.stringify(page);
        sessionStorage.setItem(storageKey, serialized);
        index[storageKey] = new TextEncoder().encode(serialized).length;
      }
    });
    savePageSizeIndex(index);
  } catch {
    // 配额清理是尽力而为；真正写入失败仍由 writePageData 的降级路径处理。
  }
}

function trimPageData(data: StoredPageData) {
  const entries = Object.entries(data.responses)
    .sort((a, b) => (a[1].cachedAt ?? 0) - (b[1].cachedAt ?? 0));
  const encoder = new TextEncoder();
  const entryBytes = entries.map(([key, value]) =>
    encoder.encode(`${JSON.stringify(key)}:${JSON.stringify(value)}`).length
  );
  let count = entries.length;
  let bytes = encoder.encode(JSON.stringify({ responses: {} })).length
    + entryBytes.reduce((sum, value) => sum + value, 0)
    + Math.max(0, count - 1);
  let index = 0;
  while (count > MAX_RESPONSES_PER_PAGE || bytes > MAX_PAGE_DATA_BYTES) {
    const oldest = entries[index];
    if (!oldest) break;
    delete data.responses[oldest[0]];
    bytes -= entryBytes[index] + (count > 1 ? 1 : 0);
    count -= 1;
    index += 1;
  }
}

function disablePageCacheWrites() {
  pageCacheWriteDisabled = true;
  window.dispatchEvent(new Event(PAGE_CACHE_STATUS_EVENT));
  if (!pageCacheWriteWarned) {
    pageCacheWriteWarned = true;
    console.warn("[pageCache] 存储空间不足，已停用本标签页的响应缓存");
  }
}

function writePageData(path: string, data: StoredPageData) {
  if (pageCacheWriteDisabled) return;
  trimPageData(data);
  try {
    const storageKey = pageDataKey(path);
    const serialized = JSON.stringify(data);
    sessionStorage.setItem(storageKey, serialized);
    const index = loadPageSizeIndex() ?? {};
    index[storageKey] = new TextEncoder().encode(serialized).length;
    savePageSizeIndex(index);
    trimGlobalPageData();
  } catch {
    // 配额不足时先淘汰当前页最旧的一半并重试一次；仍失败则显式停用并提示。
    const entries = Object.entries(data.responses)
      .sort((a, b) => (a[1].cachedAt ?? 0) - (b[1].cachedAt ?? 0));
    for (const [key] of entries.slice(0, Math.ceil(entries.length / 2))) {
      delete data.responses[key];
    }
    trimGlobalPageData(Math.floor(MAX_TOTAL_PAGE_DATA_BYTES * 0.75));
    try {
      const storageKey = pageDataKey(path);
      const serialized = JSON.stringify(data);
      sessionStorage.setItem(storageKey, serialized);
      const index = loadPageSizeIndex() ?? {};
      index[storageKey] = new TextEncoder().encode(serialized).length;
      savePageSizeIndex(index);
      trimGlobalPageData();
    } catch {
      disablePageCacheWrites();
    }
  }
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  return (init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
}

function isJsonReadRequest(url: string, method: string, init?: RequestInit): boolean {
  if (method === "GET") {
    return !isAuthProbeRequest(url)
      && !url.includes("/auth/logout")
      // 同时覆盖 /status 与 transform-status、sync-status 等轮询端点。
      && !isBackgroundStatusRequest(url)
      // 原始考勤、考勤结果与同步日志是实时列表，显式刷新必须回源。
      && !isLiveDataRequest(url);
  }
  if (method !== "POST") return false;
  if (url.includes("/export/sheet-preview")) return true;
  if (url.endsWith("/export") && String(init?.body || "").includes('"format":"meta"')) return true;
  if (url.includes("/dingtalk-employee/differences/preview")) return true;
  return false;
}

function isCacheBustingRequest(url: string, method: string, init?: RequestInit): boolean {
  return method !== "GET" && !isJsonReadRequest(url, method, init);
}

let fetchCacheInstalled = false;

/** 同一浏览器标签页内缓存页面 JSON 数据；sessionStorage 随标签页会话结束而销毁。
 *  identity 可选；安装时通常还拿不到身份（getMe 异步），随后由 setCacheIdentity 补上。 */
export function installAdminPageResponseCache(identity = "") {
  cacheIdentity = identity;
  if (fetchCacheInstalled || typeof window === "undefined") return;
  fetchCacheInstalled = true;
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = requestUrl(input);
    const path = cacheOwnerPath(url, currentAdminPath());
    const method = requestMethod(input, init);
    if (!url.includes("/api/v1/")) {
      return originalFetch(input, init);
    }
    // 写操作无论发生在哪个页面，都可能使其它页面的缓存陈旧。
    if (isCacheBustingRequest(url, method, init)) {
      clearAllSessionData();
      try {
        return await originalFetch(input, init);
      } finally {
        // 写请求飞行期间可能产生旧 GET 响应；完成（成功或失败）后再次清空。
        clearAllSessionData();
      }
    }
    if (!CACHEABLE_ADMIN_PATHS.has(path)) return originalFetch(input, init);
    const isRead = isJsonReadRequest(url, method, init);
    if (!isRead) {
      // /me、状态轮询等 GET 只是不缓存，不代表数据发生了写入。
      return originalFetch(input, init);
    }
    const bodyKey = method === "GET" ? "" : String(init?.body || "");
    const cacheKey = `${method} ${url} ${bodyKey}`;
    const pageData = readPageData(path);
    const stored = pageData.responses[cacheKey];
    if (stored) {
      return new Response(stored.body, {
        status: stored.status,
        statusText: stored.statusText,
        headers: [...stored.headers, ["x-fdc-page-cache", "session"]],
      });
    }
    const requestEpoch = cacheEpoch;
    const requestPathEpoch = pathCacheEpoch.get(path) ?? 0;
    const response = await originalFetch(input, init);
    const contentType = response.headers.get("content-type") || "";
    if (response.ok && contentType.includes("application/json")) {
      const clone = response.clone();
      const body = await clone.text();
      // 单条响应体积上限：超过则跳过缓存该响应，避免撑爆 sessionStorage。
      if (
        requestEpoch === cacheEpoch
        && requestPathEpoch === (pathCacheEpoch.get(path) ?? 0)
        && cacheOwnerPath(url, currentAdminPath()) === path
        && new TextEncoder().encode(body).length <= MAX_RESPONSE_BODY_BYTES
      ) {
        // 写回时重新读取，避免并发响应用请求发起前的快照复活已失效条目。
        const currentPageData = readPageData(path);
        currentPageData.responses[cacheKey] = {
          status: response.status,
          statusText: response.statusText,
          headers: [...response.headers.entries()],
          body,
          cachedAt: Date.now(),
        };
        writePageData(path, currentPageData);
      }
    }
    return response;
  };
}

/** getMe 完成后更新缓存身份；身份变化时清空旧身份缓存，避免换账号读到上一身份数据。 */
export function setCacheIdentity(identity: string) {
  if (identity === cacheIdentity) return;
  clearAllSessionData();
  cacheIdentity = identity;
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(PAGE_CACHE_IDENTITY_EVENT));
  }
}

export function usePageCache() {
  const value = useContext(PageCacheContext);
  if (!value) throw new Error("usePageCache must be used inside PageCacheProvider");
  return value;
}

export const NavigationTestEnvironmentContext = createContext(false);

export function CachedNavLink({ to, children }: { to: string; children: React.ReactNode }) {
  const testEnvironment = useContext(NavigationTestEnvironmentContext);
  const loc = useLocation();
  const { entries, clear } = usePageCache();
  const active = loc.pathname === to;
  const cached = entries.some((entry) => entry.path === to);

  if (!cached && !active) {
    return <Link to={to} style={{ color: "inherit" }}>{children}</Link>;
  }
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        padding: "7px 7px 6px 12px",
        border: active ? "1px solid rgba(255,255,255,.95)" : (testEnvironment ? "1px solid #ad8b00" : "1px solid rgba(255,255,255,.24)"),
        borderBottomColor: active ? "#fff" : (testEnvironment ? "#ad8b00" : "rgba(255,255,255,.24)"),
        borderRadius: "10px 10px 3px 3px",
        background: active ? "#fff" : "rgba(255,255,255,.13)",
        color: active ? "#1f1f1f" : (testEnvironment ? "#3d3000" : "rgba(255,255,255,.78)"),
        boxShadow: active ? "0 2px 7px rgba(0,0,0,.14)" : "none",
        opacity: active ? 1 : .82,
      }}
    >
      <Link to={to} style={{ color: "inherit" }}>{children}</Link>
      <button
        type="button"
        title="清除此页面缓存"
        aria-label={`清除${String(children)}缓存`}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          clear(to, active);
        }}
        style={{
          border: 0,
          width: 18,
          height: 18,
          padding: 0,
          borderRadius: "50%",
          background: "transparent",
          color: "inherit",
          opacity: active ? 0.72 : 0.58,
          cursor: "pointer",
          lineHeight: 1,
          fontSize: 15,
        }}
      >
        ×
      </button>
    </span>
  );
}

export function CachedAdminNavLink({ to, children }: { to: string; children: React.ReactNode }) {
  const testEnvironment = useContext(NavigationTestEnvironmentContext);
  const loc = useLocation();
  const { entries, clear } = usePageCache();
  const active = loc.pathname === to;
  const cached = entries.some((entry) => entry.path === to);
  if (!cached && !active) {
    return <Link to={to} style={{ color: testEnvironment ? "#614700" : "#2f54eb" }}>{children}</Link>;
  }
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 7,
      padding: "7px 7px 6px 12px", borderRadius: "10px 10px 3px 3px",
      border: testEnvironment ? "1px solid #d4b106" : (active ? "1px solid #adc6ff" : "1px solid #d6e4ff"),
      borderBottomColor: active ? "#fff" : (testEnvironment ? "#d4b106" : "#d6e4ff"),
      background: active ? "#fff" : (testEnvironment ? "#fff1b8" : "#e9effa"),
      boxShadow: active ? "0 2px 7px rgba(47,84,235,.16)" : "none",
      opacity: active ? 1 : .72,
    }}>
      <Link to={to} style={{ color: active ? "#1f1f1f" : (testEnvironment ? "#614700" : "#597ef7") }}>{children}</Link>
      <button type="button" title="清除此页面缓存" onClick={(event) => {
        event.preventDefault(); event.stopPropagation(); clear(to, active);
      }} style={{ border: 0, width: 18, height: 18, padding: 0, borderRadius: "50%", background: "transparent", color: "inherit", opacity: .6, cursor: "pointer", lineHeight: 1, fontSize: 15 }}>×</button>
    </span>
  );
}

export function PageCacheStamp({ cachedAt }: { cachedAt: number }) {
  const [writeDisabled, setWriteDisabled] = useState(pageCacheWriteDisabled);
  useEffect(() => {
    const update = () => setWriteDisabled(pageCacheWriteDisabled);
    window.addEventListener(PAGE_CACHE_STATUS_EVENT, update);
    return () => window.removeEventListener(PAGE_CACHE_STATUS_EVENT, update);
  }, []);
  const text = new Date(cachedAt).toLocaleString("zh-CN", { hour12: false });
  return (
    <div style={{ marginBottom: 8, color: writeDisabled ? "#cf1322" : "#8c8c8c", opacity: .68, fontSize: 12 }}>
      {writeDisabled
        ? "页面响应缓存空间不足，本标签页缓存已停用；清除页面缓存后会自动恢复"
        : `页面缓存于 ${text} · 切换导航时保留当前展示`}
    </div>
  );
}
