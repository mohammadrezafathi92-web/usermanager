import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // 5173 is Vite's default and therefore the most likely port to already
    // be taken by another project on the same machine. Override per-run with
    // VITE_PORT=xxxx npm run dev
    port: Number(process.env.VITE_PORT) || 5180,
    proxy: {
      "/api": {
        // Defaults to a backend running locally. Set VITE_PROXY_TARGET to
        // review UI changes against a real deployment's data instead - an
        // empty local database renders every table in its empty state, which
        // is useless for judging a layout:
        //   VITE_PROXY_TARGET=https://panel.netcip.ir npm run dev
        // Deliberately an env var rather than an edited-in URL, so nothing
        // has to be remembered and reverted (and no live panel address can
        // be committed by accident).
        target: process.env.VITE_PROXY_TARGET || "http://localhost:8000",
        changeOrigin: true,
        secure: true,
      },
    },
  },
});
