import { defineConfig } from "@playwright/test";
import path from "node:path";

/**
 * End-to-end tests run against an ISOLATED instance: its own data directory (temp), API on
 * :8100 and dashboard on :3100, so a developer's real database is never touched. The local
 * AI services (Ollama, whisper-server, Piper) are shared because they are stateless.
 */
const ROOT = path.resolve(__dirname, "../..");
const DATA = process.env.E2E_DATA_DIR ?? path.join(ROOT, ".run", "e2e-data");
const API_PORT = 8100;
const WEB_PORT = 3100;
const env = {
  VA_DATA_DIR: DATA,
  VA_PORT: String(API_PORT),
  VA_SECRET_KEY: "e2e-secret-key-not-for-production-000000000000",
  VA_CORS_ORIGINS: `["http://localhost:${WEB_PORT}","http://127.0.0.1:${WEB_PORT}"]`,
  VA_PUBLIC_CALLS_ENABLED: "true",
  PYTHONPATH: `${ROOT}/backend:${ROOT}`,
};

export default defineConfig({
  testDir: path.join(__dirname, "e2e"),
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    channel: process.env.PW_CHANNEL ?? "chrome",
    permissions: ["microphone"],
    launchOptions: {
      args: [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        `--use-file-for-fake-audio-capture=${path.join(DATA, "caller.wav")}`,
        "--autoplay-policy=no-user-gesture-required",
      ],
    },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: `${__dirname}/e2e/serve-api.sh ${DATA} ${API_PORT}`,
      cwd: `${ROOT}/backend`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      env,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `npx next dev -H 127.0.0.1 -p ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}/login`,
      env: { VA_API_ORIGIN: `http://127.0.0.1:${API_PORT}`, NEXT_TELEMETRY_DISABLED: "1", NEXT_DIST_DIR: ".next-e2e" },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
