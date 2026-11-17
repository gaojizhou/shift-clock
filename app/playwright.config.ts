import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.VERIFY_BASE_URL ?? "http://127.0.0.1:3000";
const h5Base = process.env.DINGTALK_H5_BASE_URL ?? `${baseURL}/h5`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  outputDir: "../verify-output/pw",
  projects: [
    { name: "admin", testMatch: /admin\/.*.spec.ts/ },
    {
      name: "h5",
      testMatch: /h5\/.*.spec.ts/,
      use: { baseURL: h5Base },
    },
  ],
});
