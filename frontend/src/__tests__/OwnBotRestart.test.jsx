// «راه‌اندازی مجدد ربات من»: restarts only the caller's own bot through the
// existing endpoint, saves nothing and keeps what is typed on the card.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const LIST = new Set(["fetchApiKeys", "fetchAdmins", "fetchIpBans", "fetchBackups", "fetchPaymentCards", "listTunnelNodes",
  "fetchApprovalRecoveries", "fetchProvisioningOrphans", "fetchMyBackups"]);
let botState = { running: false, last_error: "تلگرام در دسترس نیست", bot_username: "shop_bot" };
vi.mock("../api/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  const mocked = {};
  for (const [name, value] of Object.entries(actual)) {
    if (typeof value !== "function") { mocked[name] = value; continue; }
    mocked[name] = vi.fn(() => {
      if (name === "fetchMyBot") {
        return Promise.resolve({ data: { bot_token: "12345:saved", enabled: true, telegram_id_linked: true,
          required_channel_id: null, required_channel_url: null, customer_terms_text: null, ...botState } });
      }
      if (name === "restartMyBot") { botState = { ...botState, running: true, last_error: null }; }
      return Promise.resolve({ data: LIST.has(name) ? [] : {} });
    });
  }
  return mocked;
});
vi.mock("../components/Layout.jsx", () => ({ default: ({ children }) => <div>{children}</div> }));
vi.mock("../components/Topbar.jsx", () => ({ default: () => null }));
vi.mock("../components/ConfirmDialog.jsx", () => ({ useConfirm: () => async () => true }));
vi.mock("../context/AuthContext.jsx", () => ({
  useAuth: () => ({ isSuperadmin: false, isAdminOrAbove: false, username: "yasmin", telegramId: 1, refreshMe: async () => {} }),
}));

import Settings from "../pages/Settings.jsx";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import { translate } from "../i18n/translations.js";
import * as api from "../api/client.js";

const memory = new Map();
vi.stubGlobal("localStorage", {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null), setItem: (k, v) => memory.set(k, String(v)),
  removeItem: (k) => memory.delete(k), clear: () => memory.clear(),
});
const t = (key, vars) => translate("fa", key, vars);

beforeEach(() => { window.location.hash = "#bot"; botState = { running: false, last_error: "تلگرام در دسترس نیست", bot_username: "shop_bot" }; });
afterEach(() => cleanup());

describe("restart my bot", () => {
  it("restarts only through the restart endpoint, saves nothing, keeps the typed draft", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<LanguageProvider><Settings /></LanguageProvider>);
    const button = await screen.findByRole("button", { name: t("settings.restartMyBot") });
    const tokenField = screen.getByDisplayValue("12345:saved");
    await user.clear(tokenField);
    await user.type(tokenField, "99999:draft");
    await user.click(button);
    await waitFor(() => expect(screen.getByText(t("settings.myBotRestarted"))).toBeTruthy(), { timeout: 5000 });
    expect(api.restartMyBot).toHaveBeenCalledTimes(1);
    expect(api.updateMyBot).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue("99999:draft")).toBeTruthy();
    vi.useRealTimers();
  });
});
