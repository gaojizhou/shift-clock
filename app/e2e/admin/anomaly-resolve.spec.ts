import { test, expect } from "@playwright/test";

test("L3-07 异常证据链+处置", async ({ page }) => {
  await page.goto("/#/anomalies");
  await expect(page.getByTestId("anomalies-title")).toBeVisible();
  await page.getByRole("button", { name: "证据链" }).first().click();
  await expect(page.getByTestId("evidence-laoqin")).toBeVisible();
  await page.getByRole("button", { name: /计\s*入/ }).click();
});
