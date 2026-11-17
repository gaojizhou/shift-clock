import { test, expect } from "@playwright/test";

test("L3-06 H5 我的考勤日历+补录", async ({ page }) => {
  const h5Base = process.env.DINGTALK_H5_BASE_URL ?? "http://127.0.0.1:3000/h5";
  await page.request.get(`${h5Base}/test/auth?userid=T0003&role=worker`);
  await page.goto(`${h5Base}/#/calendar`);
  await expect(page.getByText("我的考勤")).toBeVisible();
  await page.getByTestId("cal-day-8").click();
  await expect(page.getByText("漏扫补录")).toBeVisible();
});
