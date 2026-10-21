// Settings page structure and safety: tabs, labels, masked API keys, the
// support save button, automatic approval in one place, and no save over
// unloaded values. Every API call is a mock.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within, waitFor, cleanup, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const LIST = new Set(["fetchApiKeys", "fetchAdmins", "fetchIpBans", "fetchBackups", "fetchPaymentCards", "listTunnelNodes",
  "fetchApprovalRecoveries", "fetchProvisioningOrphans"]);
const DATA = {
  fetchApiKeys: [{ id: 1, label: "integration", key: "umk_live_0123456789abcdefSECRET", key_type: "global_integration",
                   enabled: true, created_at: "2026-10-01T00:00:00", last_used_at: null }],
  fetchPanelSettings: { payment_card_number: "6037", support_contact_text: "@support", panel_public_url: "https://p.test",
                        panel_web_port: 8080, ha_enabled: false },
  fetchTelegramBotSettings: { bot_token: "", admin_ids: "", approval_chat_ids: "", enabled: true, auto_approve_enabled: true,
                              auto_approve_ignore_hours: false, auto_approve_from_hour: 9, auto_approve_to_hour: 21,
                              auto_approve_max_amount: 500000, auto_approve_returning_only: true, running: true },
  fetchApprovalRuntime: { requested_auto_rate_limit_mode: "shadow", effective_auto_rate_limit_mode: "off",
                          effective_registration_mode: "required", key_identity_ready: true },
};
let failPanelSettings = false;
vi.mock("../api/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  const mocked = {};
  for (const [name, value] of Object.entries(actual)) {
    if (typeof value !== "function") { mocked[name] = value; continue; }
    mocked[name] = vi.fn(() => {
      if (name === "fetchPanelSettings" && failPanelSettings) return Promise.reject(new Error("offline"));
      return Promise.resolve({ data: DATA[name] ?? (LIST.has(name) ? [] : {}) });
    });
  }
  return mocked;
});
vi.mock("../components/Layout.jsx", () => ({ default: ({ children }) => <div>{children}</div> }));
vi.mock("../components/Topbar.jsx", () => ({ default: () => null }));
vi.mock("../components/ConfirmDialog.jsx", () => ({ useConfirm: () => async () => true }));
let role = "superadmin";
vi.mock("../context/AuthContext.jsx", () => ({
  useAuth: () => ({ isSuperadmin: role === "superadmin", isAdminOrAbove: role !== "seller", username: "root",
                    telegramId: null, refreshMe: async () => {} }),
}));

import Settings from "../pages/Settings.jsx";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import { translate } from "../i18n/translations.js";
import * as api from "../api/client.js";

const memory = new Map();
vi.stubGlobal("localStorage", {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
  removeItem: (key) => memory.delete(key),
  clear: () => memory.clear(),
});
const t = (key, vars) => translate("fa", key, vars);

function show() {
  document.documentElement.setAttribute("dir", "rtl");
  return render(<LanguageProvider><Settings /></LanguageProvider>);
}
const tab = (key) => screen.getByRole("tab", { name: t(key) });

beforeEach(() => { role = "superadmin"; failPanelSettings = false; window.location.hash = ""; memory.clear(); });
afterEach(() => cleanup());

describe("settings tabs", () => {
  it("are a real tablist: one selected, roving focus, RTL arrow keys, Home/End", async () => {
    show();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((el) => el.textContent.trim())).toEqual([
      "settings.tabAccount", "settings.tabPayment", "settings.tabBotApproval", "settings.tabNetwork",
      "settings.tabDataRecovery", "settings.tabSecurity", "settings.tabOps"].map((k) => t(k)));
    expect(tab("settings.tabAccount").getAttribute("aria-selected")).toBe("true");
    expect(tabs.filter((el) => el.tabIndex === 0)).toHaveLength(1);
    const panel = screen.getByRole("tabpanel");
    expect(panel.getAttribute("aria-labelledby")).toBe(tab("settings.tabAccount").id);
    tab("settings.tabAccount").focus();
    fireEvent.keyDown(document.activeElement, { key: "ArrowLeft" });   // RTL: left = next
    expect(document.activeElement).toBe(tab("settings.tabPayment"));
    expect(tab("settings.tabPayment").getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(document.activeElement, { key: "End" });
    expect(document.activeElement).toBe(tab("settings.tabOps"));
    fireEvent.keyDown(document.activeElement, { key: "Home" });
    expect(document.activeElement).toBe(tab("settings.tabAccount"));
  });

  it("a reseller sees only the tabs with something of their own in them", async () => {
    role = "seller";
    const user = userEvent.setup();
    show();
    expect(screen.getAllByRole("tab").map((el) => el.textContent.trim())).toEqual(
      ["settings.tabAccount", "settings.tabPayment", "settings.tabBotApproval", "settings.tabDataRecovery"].map((k) => t(k)));
    await user.click(tab("settings.tabPayment"));
    expect(await screen.findByRole("button", { name: t("settings.savePaymentAndSupport") })).toBeTruthy();
    expect(screen.queryByRole("button", { name: t("settings.saveSupportText") })).toBeNull();
  });

  it("keeps a half-typed form when switching tabs", async () => {
    const user = userEvent.setup();
    show();
    const field = await screen.findByLabelText(t("settings.newUsername"));
    await user.type(field, "draft-name");
    await user.click(tab("settings.tabPayment"));
    await user.click(tab("settings.tabAccount"));
    expect(screen.getByLabelText(t("settings.newUsername")).value).toBe("draft-name");
  });
});

describe("payment and support", () => {
  it("names the support save button for what it saves", async () => {
    const user = userEvent.setup();
    show();
    await user.click(tab("settings.tabPayment"));
    expect(await screen.findByRole("button", { name: t("settings.saveSupportText") })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: t("settings.savePaymentInfo") })).toHaveLength(1);
    expect(screen.getByLabelText(t("settings.supportText")).value).toBe("@support");
  });

  it("does not offer to save over panel settings that failed to load", async () => {
    failPanelSettings = true;
    const user = userEvent.setup();
    show();
    await user.click(tab("settings.tabPayment"));
    expect(await screen.findByText(t("settings.panelSettingsLoadError"))).toBeTruthy();
    expect(screen.getByRole("button", { name: t("settings.saveSupportText") }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: t("settings.savePaymentInfo") }).disabled).toBe(true);
    expect(api.updatePanelSettings).not.toHaveBeenCalled();
  });
});

describe("API keys", () => {
  it("are masked by default; reveal and copy are explicit, named controls", async () => {
    const user = userEvent.setup();
    show();
    await user.click(tab("settings.tabSecurity"));
    const value = await screen.findByTestId("api-key-value-1");
    expect(value.textContent).not.toContain("SECRET");
    expect(value.textContent).toContain("••••");
    const reveal = screen.getByRole("button", { name: t("settings.showApiKey", { label: "integration" }) });
    expect(reveal.getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByRole("button", { name: t("settings.copyApiKey", { label: "integration" }) })).toBeTruthy();
    expect(screen.getByRole("button", { name: t("settings.deleteApiKey", { label: "integration" }) })).toBeTruthy();
    await user.click(reveal);
    expect(value.textContent).toContain("SECRET");
    await user.click(screen.getByRole("button", { name: t("settings.hideApiKey", { label: "integration" }) }));
    expect(value.textContent).not.toContain("SECRET");
    expect(api.deleteApiKey).not.toHaveBeenCalled();
  });

  it("wraps instead of overflowing a phone-width screen", async () => {
    const user = userEvent.setup();
    show();
    await user.click(tab("settings.tabSecurity"));
    const value = await screen.findByTestId("api-key-value-1");
    expect(value.className).toContain("break-all");
    const row = value.closest(".rounded-xl");
    expect(row.className).toContain("flex-col");
    expect(row.lastElementChild.className).toContain("flex-wrap");
  });
});

describe("automatic approval", () => {
  it("bot switch, its saved rules and the 60-minute limit are on the same tab", async () => {
    const user = userEvent.setup();
    show();
    await user.click(tab("settings.tabBotApproval"));
    const panel = screen.getByRole("tabpanel");
    expect(await within(panel).findByText(t("settings.autoApproveLabel"))).toBeTruthy();
    expect(await within(panel).findByText(t("wallet.autoTitle"))).toBeTruthy();
    expect(within(panel).getByText(t("settings.autoApprovalOn"))).toBeTruthy();
    expect(within(panel).getByText(t("settings.autoApprovalHours", { from: "09:00", to: "21:00" }))).toBeTruthy();
    expect(within(panel).getByText(t("wallet.autoHelp"))).toBeTruthy();
    await user.click(tab("settings.tabOps"));
    await waitFor(() => expect(screen.getByRole("tabpanel").textContent).toContain(t("settings.autoApprovalMovedHint")));
    expect(screen.getByRole("tabpanel").textContent).not.toContain(t("wallet.autoHelp"));
  });
});
