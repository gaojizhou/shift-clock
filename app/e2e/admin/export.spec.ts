import { test, expect } from "@playwright/test";

test("L3-03 月度导出", async ({ page }) => {
  await page.goto("/#/export");
  await expect(page.getByTestId("export-title")).toBeVisible();
  await page.getByTestId("export-btn").click();
  await expect(page.getByTestId("export-success")).toContainText("导出成功", {
    timeout: 15000,
  });
});
