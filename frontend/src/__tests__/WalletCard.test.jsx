import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import WalletCard from "../components/WalletCard.jsx";
import { translate } from "../i18n/translations.js";
import * as api from "../api/client.js";

vi.mock("../api/client.js", () => ({
  fetchWalletStatus: vi.fn(), fetchWalletPreflight: vi.fn(), fetchWalletReconcile: vi.fn(),
  walletCutover: vi.fn(), walletRollback: vi.fn(), fetchApprovalRuntime: vi.fn(),
  activateApprovalRuntime: vi.fn(), deactivateApprovalRuntime: vi.fn(), updateAutoApprovalRateMode: vi.fn(),
}));
const current = (changes = {}) => ({
  requested_registration_mode: "required", effective_registration_mode: "required",
  key_identity_ready: true, requested_auto_rate_limit_mode: "shadow",
  effective_auto_rate_limit_mode: "shadow", loyalty_timing_mode: "payment_event", ...changes,
});
const show = (lang = "en") => render(<WalletCard t={(key, vars) => translate(lang, key, vars)} />);
const save = () => screen.getByRole("button", { name: "Save automatic approval limit" });

beforeEach(() => {
  vi.resetAllMocks();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  api.fetchWalletStatus.mockResolvedValue({ data: { phase: "enforced" } });
  api.fetchWalletPreflight.mockResolvedValue({ data: { ready: true, blockers: [] } });
  api.fetchWalletReconcile.mockResolvedValue({ data: { ok: true, count: 0 } });
  api.fetchApprovalRuntime.mockResolvedValue({ data: current() });
  api.updateAutoApprovalRateMode.mockResolvedValue({ data: {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("hourly automatic approval setting", () => {
  it("shows requested/effective states separately without enabling anything on load", async () => {
    api.fetchApprovalRuntime.mockResolvedValue({ data: current({ effective_auto_rate_limit_mode: "off" }) });
    show();
    const select = await screen.findByRole("combobox", { name: "Automatic approval limit mode" });
    await waitFor(() => expect(select.value).toBe("shadow"));
    expect(select.disabled).toBe(false);
    const section = screen.getByRole("region", { name: "Automatic approval limit per 60 minutes" });
    expect(section.classList.contains("dark:text-gray-100")).toBe(true);
    expect(within(section).getByText("Requested mode").nextSibling.textContent).toBe("Monitor without enforcing the limit");
    expect(within(section).getByText("Current effective mode").nextSibling.textContent).toBe("Off");
    expect(save().disabled).toBe(true);
    expect(api.updateAutoApprovalRateMode).not.toHaveBeenCalled();
    expect(section.textContent).toContain("rolling 60-minute window");
    expect(section.textContent).toContain("Wallet top-ups are excluded");
    expect(section.textContent).toContain("does not schedule approval of queued receipts");
  });

  it("asks for second confirmation, submits only the requested mode and refreshes effective status", async () => {
    show();
    const select = screen.getByRole("combobox", { name: "Automatic approval limit mode" });
    await waitFor(() => expect(select.value).toBe("shadow"));
    fireEvent.change(select, { target: { value: "enforced" } });
    api.fetchApprovalRuntime.mockResolvedValue({ data: current({ requested_auto_rate_limit_mode: "enforced", effective_auto_rate_limit_mode: "enforced" }) });
    fireEvent.click(save());
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Enforce one approval per 60 minutes"));
    await waitFor(() => expect(api.updateAutoApprovalRateMode).toHaveBeenCalledTimes(1));
    expect(api.updateAutoApprovalRateMode).toHaveBeenCalledWith("enforced");
    expect(await screen.findByRole("status")).toHaveProperty("textContent", "Mode saved; check the effective status above.");
    expect(select.value).toBe("enforced");
    expect(api.activateApprovalRuntime).not.toHaveBeenCalled();
  });

  it("cancel sends no request", async () => {
    show();
    const select = screen.getByRole("combobox");
    await waitFor(() => expect(select.value).toBe("shadow"));
    fireEvent.change(select, { target: { value: "enforced" } });
    window.confirm.mockReturnValue(false);
    fireEvent.click(save());
    expect(api.updateAutoApprovalRateMode).not.toHaveBeenCalled();
  });

  it.each([
    { effective_registration_mode: "shadow" },
    { key_identity_ready: false },
  ])("disables editing and explains unmet readiness: %o", async (changes) => {
    api.fetchApprovalRuntime.mockResolvedValue({ data: current(changes) });
    show();
    const select = screen.getByRole("combobox");
    await waitFor(() => expect(select.value).toBe("shadow"));
    expect(select.disabled).toBe(true);
    expect(save().disabled).toBe(true);
    expect(screen.getByText("Changing this setting requires effective required registration and a ready key identity.")).toBeTruthy();
    expect(api.updateAutoApprovalRateMode).not.toHaveBeenCalled();
  });

  it("prevents a duplicate request while busy and reports an API error without false success", async () => {
    let reject;
    api.updateAutoApprovalRateMode.mockImplementation(() => new Promise((resolve, no) => { reject = no; }));
    show();
    const select = screen.getByRole("combobox");
    await waitFor(() => expect(select.value).toBe("shadow"));
    fireEvent.change(select, { target: { value: "enforced" } });
    fireEvent.click(save());
    fireEvent.click(screen.getByRole("button", { name: "Saving…" }));
    expect(api.updateAutoApprovalRateMode).toHaveBeenCalledTimes(1);
    expect(select.disabled).toBe(true);
    reject({ response: { data: { detail: "key_identity_not_ready" } } });
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "key_identity_not_ready");
    await waitFor(() => expect(save().disabled).toBe(false));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("renders localized Persian controls and scope", async () => {
    show("fa");
    const select = screen.getByRole("combobox", { name: "حالت سقف تأیید خودکار" });
    await waitFor(() => expect(select.value).toBe("shadow"));
    expect(screen.getByRole("button", { name: "ذخیرهٔ سقف تأیید خودکار" })).toBeTruthy();
    expect(screen.getByText(/بازهٔ شناور ۶۰ دقیقه/)).toBeTruthy();
  });
});
