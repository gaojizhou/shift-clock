import { test, expect } from "@playwright/test";

test("L3-04 H5 Mock Auth 扫码上岗", async ({ page }) => {
  const h5Base = process.env.DINGTALK_H5_BASE_URL ?? "http://127.0.0.1:3000/h5";
  const authRes = await page.request.get(`${h5Base}/test/auth?userid=T0005`);
  const authJson = await authRes.json();
  expect(authJson.userid).toBe("T0005");

  await page.addInitScript(() => {
    (window as unknown as { __TEST_SCAN__?: string }).__TEST_SCAN__ = "示例厂区B|35号工作点";
  });
  await page.goto(`${h5Base}/#/scan?userid=T0005`);

  await expect(page.getByTestId("post-select")).toBeVisible();
  await expect(page.getByTestId("squad-3班")).toBeVisible();
  await expect(page.getByTestId("quota-出料工")).toContainText("正式岗");

  await page.getByTestId("post-出料工").click();
  await page.getByTestId("squad-3班").click();
  // 默认用工类型为「实习」；勿点 Selector 标签（会清空 value 导致 422）
  await expect(page.getByTestId("employ-type")).toContainText("实习");
  await page.getByTestId("submit-scan").click();

  await expect(page.getByTestId("scan-success")).toContainText(/上岗成功|已上岗/);
  await expect(page.getByTestId("scan-success")).toContainText("35号工作点");
});
