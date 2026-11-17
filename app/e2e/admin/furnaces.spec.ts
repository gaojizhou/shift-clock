import { test, expect } from "@playwright/test";

test("L3-01 工作点编制配置页", async ({ page }) => {
  await page.goto("/#/furnaces");
  await expect(page.getByTestId("furnaces-title")).toBeVisible();
  await expect(page.getByTestId("furnace-shz-1")).toContainText("示例厂区A");
  await expect(page.getByTestId("furnace-shz-1")).toContainText("1号工作点");
  await expect(page.getByTestId("furnace-shz-1")).toContainText("标准型A");
  await expect(page.getByText("无班组").first()).toBeVisible();
});
