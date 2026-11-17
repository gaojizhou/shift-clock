import assert from "node:assert/strict";
import test from "node:test";

import {
  CACHEABLE_ADMIN_PATHS,
  cacheOwnerPath,
  isAuthProbeRequest,
  isBackgroundStatusRequest,
  isLiveDataRequest,
} from "../src/pageCachePolicy.ts";

test("keeps the real /api/v1/me identity probe out of page caching", () => {
  assert.equal(isAuthProbeRequest("/api/v1/me"), true);
  assert.equal(isAuthProbeRequest("/api/v1/me?refresh=1"), true);
  assert.equal(isAuthProbeRequest("/api/v1/employees"), false);
});

test("keeps live administration pages out of page caching", () => {
  assert.equal(CACHEABLE_ADMIN_PATHS.has("/employee-sync"), false);
  assert.equal(CACHEABLE_ADMIN_PATHS.has("/connection-pool"), false);
  assert.equal(CACHEABLE_ADMIN_PATHS.has("/raw-attendance-sync"), true);
  assert.equal(CACHEABLE_ADMIN_PATHS.has("/shift-push"), true);
});

test("keeps background status polling responses out of the page cache", () => {
  const liveStatusUrls = [
    "/api/v1/admin/dingtalk-attendance/transform-status",
    "/api/v1/admin/dingtalk-employee/sync-status",
    "/api/v1/admin/dingtalk-attendance/result/status",
    "/api/v1/admin/dingtalk-employee/manual-sync/status?id=1",
    "/api/v1/admin/shift-push/furnaces",
    "/api/v1/admin/shift-push/records?limit=100",
  ];

  for (const url of liveStatusUrls) {
    assert.equal(isBackgroundStatusRequest(url), true, url);
  }
  assert.equal(isBackgroundStatusRequest("/api/v1/employees?status=active"), false);
});

test("keeps raw attendance lists live so refresh always reaches the server", () => {
  for (const url of [
    "/api/v1/export/absence-overrides?month=2026-04&furnaceIds=1",
    "/api/v1/admin/connection-pool",
    "/api/v1/admin/dingtalk-attendance/raw?dateFrom=2026-08-01",
    "/api/v1/admin/dingtalk-attendance/result?dateFrom=2026-08-01",
    "/api/v1/admin/dingtalk-attendance/sync-logs?limit=100",
    "/api/v1/admin/sync-execution-logs?group=attendance&page=1",
  ]) {
    assert.equal(isLiveDataRequest(url), true, url);
  }
  assert.equal(isLiveDataRequest("/api/v1/furnaces"), false);
});

test("attributes delayed requests to their business page instead of the visible hash", () => {
  assert.equal(
    cacheOwnerPath("/api/v1/export/sheet-preview", "/anomalies"),
    "/export",
  );
  assert.equal(
    cacheOwnerPath("/api/v1/recon/timeline?month=2026-08", "/export"),
    "/anomalies",
  );
  assert.equal(cacheOwnerPath("/api/v1/furnaces", "/furnaces"), "/furnaces");
});
