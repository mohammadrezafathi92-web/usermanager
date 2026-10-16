import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Focused UI tests (npm test): jsdom, no network - every API call is mocked.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/__tests__/**/*.test.jsx"],
    restoreMocks: true,
  },
});
