export const CACHEABLE_ADMIN_PATHS = new Set([
  "/furnaces", "/anomalies", "/escalations", "/proxy-quota", "/export",
  "/raw-attendance-sync", "/dictionaries", "/employees",
  "/audit-logs", "/shift-push",
]);

// 所有受支持的管理页路径 = 可缓存页 + 需实时回源（不缓存）但仍有渲染入口的页。
// CachedAdminRoutes 的兜底 Routes 用它注册非可缓存页，避免命中通配路由被弹到 /forbidden。
export const SUPPORTED_ADMIN_PATHS = new Set([
  ...CACHEABLE_ADMIN_PATHS,
  "/employee-sync",
  "/connection-pool",
  "/dingtalk-load-test",
]);

// 页面响应缓存不按时间过期；由手动清除、写请求、身份切换或容量淘汰失效。

const BACKGROUND_STATUS_PATH_RE = /(?:^|[-/])status(?:[/?#]|$)/;
const LIVE_STATUS_PATH_PATTERNS = [
  /\/admin\/shift-push\/furnaces(?:[/?#]|$)/,
  /\/admin\/shift-push\/records(?:[/?#]|$)/,
];
const AUTH_PROBE_PATH_RE = /\/me(?:[/?#]|$)/;
const LIVE_DATA_PATH_PATTERNS = [
  /\/export\/request-status(?:[/?#]|$)/,
  /\/export\/absence-overrides(?:[/?#]|$)/,
  /\/admin\/sync-execution-logs(?:[/?#]|$)/,
  /\/admin\/connection-pool(?:[/?#]|$)/,
  /\/admin\/dingtalk-attendance\/raw(?:[/?#]|$)/,
  /\/admin\/dingtalk-attendance\/result(?:[/?#]|$)/,
  /\/admin\/dingtalk-attendance\/sync-logs(?:[/?#]|$)/,
];

const REQUEST_OWNER_PATTERNS: [RegExp, string][] = [
  [/\/export(?:[/?#]|$)/, "/export"],
  [/\/recon\/timeline(?:[/?#]|$)/, "/anomalies"],
  [/\/admin\/dingtalk-attendance\//, "/raw-attendance-sync"],
];

export function isBackgroundStatusRequest(url: string): boolean {
  return BACKGROUND_STATUS_PATH_RE.test(url)
    || LIVE_STATUS_PATH_PATTERNS.some((pattern) => pattern.test(url));
}

/** 身份探针必须始终回源，不能让过期 /me 响应延长前端会话。 */
export function isAuthProbeRequest(url: string): boolean {
  return AUTH_PROBE_PATH_RE.test(url);
}

export function isLiveDataRequest(url: string): boolean {
  return LIVE_DATA_PATH_PATTERNS.some((pattern) => pattern.test(url));
}

/** 长请求或隐藏页续发请求按业务端点归回发起页，未知共享端点才沿用当前页。 */
export function cacheOwnerPath(url: string, currentPath: string): string {
  return REQUEST_OWNER_PATTERNS.find(([pattern]) => pattern.test(url))?.[1] ?? currentPath;
}
