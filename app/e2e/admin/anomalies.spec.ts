import { test, expect } from "@playwright/test";

test("L3-02 T-2 异常工作台", async ({ page }) => {
  await page.goto("/#/anomalies?date=T-2");
  await expect(page.getByTestId("anomalies-title")).toBeVisible();
  await expect(page.getByText("劳勤有工作点无", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("工作点有劳勤无", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("时间错位", { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId("anomaly-list")).toBeVisible();
});
