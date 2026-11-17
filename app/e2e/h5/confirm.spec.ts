import { test, expect } from "@playwright/test";

test("L3-05 H5 值班长日确认四段式三日tab", async ({ page }) => {
  const h5Base = process.env.DINGTALK_H5_BASE_URL ?? "http://127.0.0.1:3000/h5";
  await page.request.get(`${h5Base}/test/auth?userid=L0001&role=chief`);
  await page.goto(`${h5Base}/#/confirm`);
  await expect(page.getByText("值班长日确认")).toBeVisible();
  await expect(page.getByText("今日")).toBeVisible();
  await expect(page.getByText("T-1")).toBeVisible();
  await expect(page.getByText("T-2")).toBeVisible();
  await expect(page.getByText("已扫")).toBeVisible();
  await expect(page.getByText("未扫")).toBeVisible();
  await expect(page.getByText("外来")).toBeVisible();
  await expect(page.getByText("待审补录")).toBeVisible();
  await expect(page.getByRole("button", { name: /全部确认提交/ })).toBeVisible();
});
