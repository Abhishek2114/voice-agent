import path from "node:path";

import { defineConfig } from "vitest/config";

export default defineConfig({
  // @vitejs/plugin-react is not installed; Vite 8's built-in Oxc transform handles JSX.
  oxc: { jsx: { runtime: "automatic" } },
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", ".next/**", ".next-e2e/**"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
