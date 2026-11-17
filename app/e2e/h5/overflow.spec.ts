import { test, expect, type Page } from "@playwright/test";

const h5Base = process.env.DINGTALK_H5_BASE_URL ?? "http://127.0.0.1:3000/h5";

// 移动端视口下断言无横向溢出。
// 防回归：页面内多处原生 button/div 使用 width:100% / calc(100% - Npx) 叠加 padding，
// 若全局盒模型回退到 content-box，会使页面与底部满宽弹窗(Popup)超出屏幕宽度、可横向滚动。
test.use({ viewport: { width: 375, height: 812 } });

async function expectNoHorizontalOverflow(page: Page, where: string) {
  const { scrollW, clientW } = await page.evaluate(() => {
    const doc = document.documentElement;
    return { scrollW: doc.scrollWidth, clientW: doc.clientWidth };
  });
  expect(scrollW, `${where} 横向溢出 ${scrollW - clientW}px`).toBeLessThanOrEqual(clientW);
}

test("L3-07 H5 移动视口无横向溢出（含弹窗）", async ({ page }) => {
  // 工人「我的考勤」：打开「漏扫补录」弹窗后仍无横向溢出
  await page.request.get(`${h5Base}/test/auth?userid=T0003&role=worker`);
  await page.goto(`${h5Base}/#/calendar`);
  await expect(page.getByText("我的考勤")).toBeVisible();
  await expectNoHorizontalOverflow(page, "calendar");
  await page.getByTestId("cal-day-8").click();
  await expect(page.getByText("漏扫补录")).toBeVisible();
  await expectNoHorizontalOverflow(page, "calendar+补录弹窗");

  // 扫码模拟页：工作点满宽卡片按钮
  await page.goto(`${h5Base}/#/scan-sim`);
  await expect(page.getByText(/手动输入二维码|扫码上岗/)).toBeVisible();
  await expectNoHorizontalOverflow(page, "scan-sim");

  // 值班长日确认页：四段式 + 底部满宽提交条
  await page.request.get(`${h5Base}/test/auth?userid=L0001&role=chief`);
  await page.goto(`${h5Base}/#/confirm`);
  await expect(page.getByText("值班长日确认")).toBeVisible();
  await expectNoHorizontalOverflow(page, "confirm");
});
