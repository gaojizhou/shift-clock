import { test, expect } from '@playwright/test';

for (const enabled of [true, false, undefined]) {
  test(`后台环境标识开关 ${String(enabled)} 保持对应导航主题`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      return route.fulfill({ json: path === '/api/v1/me'
        ? { userid: 'admin', name: '管理员', role: 'admin', super: true, adminTestBanner: enabled }
        : { items: [] } });
    });
    await page.goto('/#/forbidden');
    const header = page.getByTestId('admin-navigation');
    await expect(header).toBeVisible();
    await expect(header).toHaveCSS('background-color', enabled ? 'rgb(250, 219, 20)' : 'rgb(22, 119, 255)');
    await expect(header).toHaveCSS('color', enabled ? 'rgb(61, 48, 0)' : 'rgb(255, 255, 255)');
    await expect(page.getByTestId('test-environment-badge')).toHaveCount(enabled ? 1 : 0);
    await expect(page.getByTestId('admin-bar')).toHaveCSS('background-color', enabled ? 'rgb(255, 251, 230)' : 'rgb(240, 245, 255)');
    await expect(page.getByTestId('admin-bar').getByRole('link', { name: '员工管理', exact: true }))
      .toHaveCSS('color', enabled ? 'rgb(97, 71, 0)' : 'rgb(47, 84, 235)');
    expect(errors).toEqual([]);
  });
}
