import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { visualizer } from "rollup-plugin-visualizer";

// Прокси на FastAPI-бэкенд (pegasgap web → :8000). Весь API живёт под /api,
// поэтому одного правила достаточно.
const BACKEND = process.env.PEGASGAP_API || "http://127.0.0.1:8000";

// Bundle analyzer включается через ANALYZE=1 npm run build (или :analyze скрипт).
// Создаёт `dist/stats.html` (treemap размеров модулей в gzip).
const analyze = process.env.ANALYZE === "1";

export default defineConfig({
  plugins: [
    react(),
    analyze && visualizer({
      filename: "dist/stats.html",
      gzipSize: true,
      brotliSize: true,
      template: "treemap",
    }),
  ].filter(Boolean),
  // Относительные пути ассетов — чтобы собранный dist раздавался FastAPI
  // под префиксом /app (StaticFiles) без переписывания ссылок.
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
    },
  },
  // vitest: jsdom для component-тестов через @testing-library/react.
  // Pure-function тесты (lib/*.test.js) тоже работают (jsdom включает Node).
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.js"],
  },
});
