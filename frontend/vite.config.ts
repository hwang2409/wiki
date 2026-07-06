import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.WIKI_API_TARGET ?? "http://127.0.0.1:8011";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": apiTarget,
      "/health": apiTarget
    }
  }
});
