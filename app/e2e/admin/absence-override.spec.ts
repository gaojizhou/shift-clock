import { test, expect, type BrowserContext } from '@playwright/test';

test('手动旷工跨用户共享，0优先，冲突保护，清空恢复自动', async ({ browser }) => {
  let saved: number | null = null;
  let revision = 0;
  const setup = async (context: BrowserContext) => {
    await context.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
      const entry = () => ({ days: saved, maxDays: 4.5, revision, editable: true, reason: '' });
      if (path === '/me') return route.fulfill({ json: { userid: 'admin', name: '测试管理员', role: 'admin' } });
      if (path === '/demo-context') return route.fulfill({ json: { month: '2026-04', today: '2026-10-13' } });
      if (path === '/furnaces') return route.fulfill({ json: { items: [{ id: 101, furnaceNo: '1号工作点', region: '示例厂区A' }] } });
      if (path === '/export/button-visibility') return route.fulfill({ json: { buttons: Object.fromEntries(
        ['combined', 'payroll', 'payroll2', 'anomaly', 'tripartite', 'hours', 'overtime', 'overtimeSummary', 'overtimePerson'].map(key => [key, ['hr', 'stat']])) } });
      if (path === '/export/absence-overrides') {
        if (route.request().method() === 'PUT') {
          const body = route.request().postDataJSON();
          if (body.revision !== revision) return route.fulfill({ status: 409, json: { detail: '该旷工值已被他人修改，请刷新后重试' } });
          saved = body.days;
          revision += 1;
          return route.fulfill({ json: entry() });
        }
        return route.fulfill({ json: { items: { MANUAL_EMP: entry() } } });
      }
      if (path === '/export/sheet-preview') return route.fulfill({ json: {
        furnaceCount: 1, warnings: [], payrollSheet: { columns: ['工号', '姓名', '旷工天数'],
          rows: [{ 工号: 'MANUAL_EMP', 姓名: '手动测试员工', 旷工天数: 1.25 }] },
      } });
      return route.fulfill({ json: { items: [] } });
    });
    const page = await context.newPage();
    await page.goto('/#/export');
    await page.getByTestId('export-furnace-101').click();
    await page.getByRole('button', { name: '刷新预览', exact: true }).first().click();
    await expect(page.getByText('手动测试员工', { exact: true })).toBeVisible();
    return page;
  };
  const first = await browser.newContext();
  const second = await browser.newContext();
  try {
    const a = await setup(first);
    const b = await setup(second);
    const editor = (page: typeof a) => page.getByRole('spinbutton', { name: '手动旷工天数' });
    const save = (page: typeof a) => page.getByRole('button', { name: /保\s*存/ });
    const automatic = async (page: typeof a, value: string) => {
      const row = page.getByRole('row').filter({ has: page.getByText('手动测试员工', { exact: true }) });
      // 手动值保存、跨用户刷新后，左侧自动计算值保持不变。
      await expect(row.locator('td').filter({ has: editor(page) }).locator('xpath=preceding-sibling::td[1]')).toHaveText(value);
    };
    await expect(a.getByRole('columnheader', { name: '自动计算', exact: true })).toBeVisible();
    await expect(a.getByRole('columnheader', { name: '手动输入', exact: true })).toBeVisible();
    await expect(editor(a)).toHaveAttribute('aria-valuemax', '4.5');
    await editor(a).fill('0');
    await save(a).click();
    await expect.poll(() => saved).toBe(0);
    await automatic(a, '1.25');
    await b.getByRole('button', { name: '刷新预览', exact: true }).first().click();
    await expect(editor(b)).toHaveValue('0.00');
    await automatic(b, '1.25');
    await editor(b).fill('2.5');
    await save(b).click();
    await automatic(b, '1.25');
    await editor(a).fill('3');
    await save(a).click();
    await expect(a.getByText('该旷工值已被他人修改，请刷新后重试', { exact: true })).toBeVisible();
    await expect(editor(a)).toHaveValue('2.50');
    expect(saved).toBe(2.5);
    await editor(b).fill('');
    await save(b).click();
    await automatic(b, '1.25');
    expect(saved).toBeNull();
  } finally {
    await first.close();
    await second.close();
  }
});
