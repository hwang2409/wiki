import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    environmentOptions: {
      jsdom: {
        // Set an origin so document.URL is a real URL under test.
        url: "http://localhost/",
      },
    },
    setupFiles: ["./vitest.setup.ts"],
    include: ["tests/**/*.integration.test.tsx", "tests/**/*.unit.test.ts"],
  },
});
