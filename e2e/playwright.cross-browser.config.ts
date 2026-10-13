import { defineConfig, devices } from "@playwright/test";
import base from "./playwright.config";

if (process.env.COSCI_E2E_PRODUCTION !== "1") {
  throw new Error("Cross-browser guards require COSCI_E2E_PRODUCTION=1.");
}

export default defineConfig({
  ...base,
  testDir: "./production",
  grep: /@keyboard-(menu|stream|semantics|progress)/,
  projects: [
    {
      name: "webkit",
      use: {
        ...devices["Desktop Safari"],
        viewport: { width: 1440, height: 900 },
        launchOptions: {},
      },
    },
    {
      name: "webkit-iphone",
      testIgnore: ["**/landing-navigation.spec.ts", "**/landing-focus.spec.ts"],
      use: { ...devices["iPhone 14"], launchOptions: {} },
    },
    {
      name: "firefox",
      use: {
        ...devices["Desktop Firefox"],
        viewport: { width: 1440, height: 900 },
        launchOptions: {},
      },
    },
  ],
});
