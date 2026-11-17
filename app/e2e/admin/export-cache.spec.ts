import { test, expect } from '@playwright/test';

for (const failRefresh of [false, true]) {
  test(`月度预览长期缓存保留页面状态，刷新${failRefresh ? '失败保留旧表' : '成功更新旧表'}`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    let previewCalls = 0;
    let releaseRefresh: () => void = () => {};
    const refreshGate = new Promise<void>(resolve => { releaseRefresh = resolve; });
    await page.route('**/api/v1/**', async route => {
      const url = new URL(route.request().url());
      const path = url.pathname.replace('/api/v1', '');
      if (path === '/me') return route.fulfill({ json: { userid: 'CACHE_TEST', name: '缓存测试', role: 'admin', region: '' } });
      if (path === '/demo-context') return route.fulfill({ json: { month: '2026-10', today: '2026-10-12' } });
      if (path === '/furnaces') return route.fulfill({ json: { items: [
        { id: 101, furnaceNo: '1号工作点', region: '示例厂区B' },
        { id: 102, furnaceNo: '2号工作点', region: '示例厂区B' },
      ] } });
      if (path === '/export/button-visibility') return route.fulfill({ json: { buttons: Object.fromEntries(['combined', 'payroll', 'payroll2', 'anomaly', 'tripartite', 'hours', 'overtime', 'overtimeSummary', 'overtimePerson'].map(key => [key, ['hr', 'stat']])) } });
      if (path === '/export/sheet-preview') {
        previewCalls += 1;
        if (previewCalls > 1) {
          await refreshGate;
          if (failRefresh) return route.fulfill({ status: 503, json: { detail: '预览服务暂不可用' } });
        }
        return route.fulfill({ json: {
          furnaceCount: 2, warnings: [], payrollSheet: {
            columns: ['工号', '姓名', '实际出勤天数', '旷工天数'],
            rows: Array.from({ length: 12 }, (_, i) => ({
              工号: `CACHE_${i + 1}`, 姓名: `缓存员工${i + 1}`, 实际出勤天数: previewCalls,
              ...(i === 0 ? {} : { 旷工天数: i === 1 ? null : i === 2 ? "" : i === 4 ? 1.25 : 0 }),
            })),
          },
        } });
      }
      return route.fulfill({ json: { items: [] } });
    });
    await page.goto('/#/export');
    await page.getByTestId('export-furnace-101').click();
    await page.getByTestId('export-furnace-102').click();
    await page.getByRole('button', { name: '刷新预览', exact: true }).first().click();
    await expect(page.getByText('缓存员工1', { exact: true })).toBeVisible();
    const payrollTable = page.locator('.ant-table-wrapper').filter({ has: page.getByText('缓存员工1', { exact: true }) });
    const absenceColumn = (await payrollTable.getByRole('columnheader').allTextContents()).findIndex(text => text.includes('旷工天数'));
    expect(absenceColumn).toBeGreaterThanOrEqual(0);
    for (let employee = 1; employee <= 5; employee += 1) {
      const row = page.getByRole('row').filter({ has: page.getByText(`缓存员工${employee}`, { exact: true }) });
      await expect(row.getByRole('cell').nth(absenceColumn)).toHaveText(employee === 5 ? '1.25' : '0');
    }
    await page.getByTestId('filter-kw-payroll').fill('缓存员工');
    await page.getByTestId('filter-apply-payroll').click();
    await page.locator('.ant-pagination-item-2:visible').first().click();
    await expect(page.getByText('缓存员工11', { exact: true })).toBeVisible();

    // 普通切换不重算，也不丢分页。
    await page.evaluate(() => { window.location.hash = '/forbidden'; });
    await expect(page.getByText('缓存员工11', { exact: true })).toBeHidden();
    await page.getByRole('link', { name: '月度导出', exact: true }).click();
    await expect(page.getByText('缓存员工11', { exact: true })).toBeVisible();
    expect(previewCalls).toBe(1);

    await page.evaluate(() => { window.location.hash = '/forbidden'; });
    await expect(page.getByText('缓存员工11', { exact: true })).toBeHidden();
    await page.evaluate(() => {
      const original = Date.now;
      Date.now = () => original() + 31 * 24 * 60 * 60 * 1000;
    });
    await page.getByRole('link', { name: '月度导出', exact: true }).click();
    await expect(page.getByText('缓存员工11', { exact: true })).toBeVisible();
    expect(previewCalls).toBe(1); // 超过原先 1 分钟和 30 分钟的期限也不自动刷新。
    if (failRefresh) {
      // 本浏览器发生写入后，切回仍会重新获取已失效的数据。
      await page.evaluate(async () => {
        await fetch('/api/v1/cache-test-write', { method: 'POST' });
        window.location.hash = '/forbidden';
      });
      await expect(page.getByText('缓存员工11', { exact: true })).toBeHidden();
      await page.getByRole('link', { name: '月度导出', exact: true }).click();
    } else {
      await page.getByRole('button', { name: '刷新预览', exact: true }).first().click();
    }
    await expect.poll(() => previewCalls).toBe(2);
    // 故意挂起刷新响应：旧表必须仍在，全选范围、筛选和第二页不能丢失。
    await expect(page.getByText('缓存员工11', { exact: true })).toBeVisible();
    await expect(page.getByTestId('filter-kw-payroll')).toHaveValue('缓存员工');
    await expect(page.getByText('已选 2/2', { exact: true })).toBeVisible();
    releaseRefresh();
    if (failRefresh) {
      await expect(page.getByTestId('export-preview-error')).toContainText('预览服务暂不可用');
    } else {
      await expect(page.getByRole('row').filter({ hasText: 'CACHE_11' })).toContainText('2');
    }
    await expect(page.getByText('缓存员工11', { exact: true })).toBeVisible();
    expect(previewCalls).toBe(2); // 单表预览只刷新这一张，不能自动计算所有重表。
    expect(errors).toEqual([]);
  });
}

// 验证真实 fetch 拦截器：全部缓存页的接口响应均不按时间失效。
test('所有页面响应长期保留，写操作仍会使缓存失效', async ({ page }) => {
  let reads = 0;
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/me') {
      return route.fulfill({ json: { userid: 'CACHE_TEST', name: '缓存测试', role: 'admin' } });
    }
    if (path === '/api/v1/cache-test-read') reads += 1;
    return route.fulfill({ json: { value: reads } });
  });
  await page.goto('/#/forbidden');
  await expect(page.getByRole('link', { name: '月度导出', exact: true })).toBeVisible();
  const result = await page.evaluate(async () => {
    const paths = ['/furnaces', '/anomalies', '/escalations', '/proxy-quota', '/export',
      '/raw-attendance-sync', '/dictionaries', '/employees', '/audit-logs', '/shift-push'];
    const samples: { cached: string | null; before: number; after: number; refreshed: number }[] = [];
    const original = Date.now;
    let offset = 0;
    Date.now = () => original() + offset;
    try {
      for (const path of paths) {
        // 仅切换请求归属，不挂载与本用例无关的业务页面。
        history.replaceState(null, '', `#${path}`);
        const before = await (await fetch('/api/v1/cache-test-read')).json();
        offset += 31 * 24 * 60 * 60 * 1000;
        const response = await fetch('/api/v1/cache-test-read');
        const after = await response.json();
        await fetch('/api/v1/cache-test-write', { method: 'POST' });
        const refreshed = await (await fetch('/api/v1/cache-test-read')).json();
        samples.push({ cached: response.headers.get('x-fdc-page-cache'),
          before: before.value, after: after.value, refreshed: refreshed.value });
      }
    } finally {
      Date.now = original;
      history.replaceState(null, '', '#/forbidden');
    }
    return samples;
  });
  expect(result).toHaveLength(10);
  for (const sample of result) {
    expect(sample.cached).toBe('session');
    expect(sample.after).toBe(sample.before);
    expect(sample.refreshed).toBe(sample.before + 1);
  }
  expect(reads).toBe(20);
});
