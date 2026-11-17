import { test, expect } from '@playwright/test';

test('single refresh serializes absence and sheets, retries busy and shows occupying user', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  let active = 0, peak = 0, previews = 0, absence = 0, rejected = false;
  const requestedAt = new Date().toISOString();
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    if (path === '/me') return route.fulfill({json:{userid:'ADMIN01',name:'管理员',role:'admin'}});
    if (path === '/export/button-visibility') return route.fulfill({json:{buttons:Object.fromEntries(
      ['combined','payroll','payroll2','anomaly','tripartite','hours','overtime','overtimeSummary','overtimePerson'].map(key=>[key,['hr','stat']])
    )}});
    if (path === '/demo-context') return route.fulfill({json:{month:'2026-09',today:'2026-10-13'}});
    if (path === '/furnaces') return route.fulfill({json:{items:[{id:101,furnaceNo:'1号工作点',region:'示例厂区A'}]}});
    if (path === '/export/request-status') return route.fulfill({json:{capacity:1,paused:false,items:[{
      userId:'LX001',userName:'刘轩',task:'生成工作表预览 · 输出给薪资',requestedAt,elapsedSeconds:8,
    }]}});
    if (!['/export/absence-overrides','/export/sheet-preview','/export/monthly-hours','/export/overtime-detail'].includes(path)) {
      return route.fulfill({json:{items:[]}});
    }
    active++; peak=Math.max(peak,active);
    try {
      await new Promise(resolve => setTimeout(resolve, 150));
      if (path === '/export/absence-overrides') { absence++; return await route.fulfill({json:{items:{}}}); }
      if (path === '/export/sheet-preview') {
        previews++;
        if (!rejected) { rejected=true; return await route.fulfill({status:429,headers:{'Retry-After':'1'},json:{detail:'报表正在计算，请稍后重试'}}); }
        const sheet=route.request().postDataJSON().sheet;
        return await route.fulfill({json:{furnaceCount:1,warnings:[],
          ...(sheet.startsWith('输出给薪资') ? {payrollSheet:{columns:['工号','姓名','旷工天数'],rows:[{工号:'Q1',姓名:'排队测试员工',旷工天数:0}]}}
            : {extraSheet:{columns:['工号'],rows:[]}})}});
      }
      if (path === '/export/monthly-hours') return await route.fulfill({json:{columns:['工号','旷工天数'],rows:[],month:'2026-09',daysInMonth:30}});
      return await route.fulfill({json:{columns:[],rows:[],summaryColumns:[],summaryRows:[],byPersonColumns:[],byPersonRows:[]}});
    } finally { active--; }
  });
  await page.goto('/#/export');
  const status=page.getByTestId('export-request-status');
  await expect(status).toContainText('刘轩');
  await expect(status).toContainText('LX001');
  await expect(status).toContainText('发起时间：');
  await page.getByTestId('export-furnace-101').click();
  await page.getByTestId('export-preview-btn').evaluate((el: HTMLButtonElement) => { el.click(); el.click(); });
  await expect(status).toContainText('正在等待其他计算完成');
  await expect(page.getByTestId('export-preview-btn')).toHaveText('刷新全部预览', {timeout:20000});
  await expect(status).toContainText('本页没有等待中的请求');
  expect(pageErrors).toEqual([]);
  expect(peak).toBe(1);
  expect(previews).toBe(5); // 四张单表预览，加一次429重试；重复点击未创建第二条刷新链。
  expect(absence).toBeLessThanOrEqual(2);
  await expect(page.getByText('预览刷新已暂停，后续工作表尚未更新')).toHaveCount(0);
});
