// Changing one service's concurrent-session cap. Every API call is a mock.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api/client.js", () => ({
  previewPurchaseCap: vi.fn(),
  changePurchaseCap: vi.fn(),
}));

import * as api from "../api/client.js";
import PurchaseCapDialog, { parseCap } from "../components/PurchaseCapDialog.jsx";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import { translate } from "../i18n/translations.js";

const t = (key, vars) => translate("fa", key, vars);
const memory = new Map();
vi.stubGlobal("localStorage", {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
  removeItem: (key) => memory.delete(key),
  clear: () => memory.clear(),
});

const PURCHASE = { id: 7, max_concurrent_sessions: 3, reserved_max_concurrent_sessions: 4 };
const shown = (extra) => ({ purchase_id: 7, user_id: 1, current_value: 3, new_value: 5, preview_token: "tok",
  live_sessions_in_scope: 2, live_ppp_sessions: 2, live_other_sessions: 0, excess_now: 0, in_force_now: true,
  session_cap_mode: "enforced",
  busy: null, scope: [{ connection_id: 1, type: "openvpn", connection_cap: 1 }], ...extra });
const httpError = (status, detail) => Object.assign(new Error("http"), { response: { status, data: { detail } } });

function open(props = {}) {
  document.body.innerHTML = '<div id="root"></div>';
  const onDone = vi.fn();
  render(<LanguageProvider>
    <PurchaseCapDialog purchase={PURCHASE} onClose={() => {}} onDone={onDone} {...props} />
  </LanguageProvider>, { container: document.getElementById("root") });
  return onDone;
}

async function previewWith(user, value, data) {
  api.previewPurchaseCap.mockResolvedValue({ data });
  const input = screen.getByRole("spinbutton");
  await user.clear(input);
  await user.type(input, String(value));
  await user.click(screen.getByRole("button", { name: t("purchaseCap.check") }));
}

beforeEach(() => vi.resetAllMocks());
afterEach(() => cleanup());

describe("purchase cap dialog", () => {
  it("accepts only whole numbers 1..1000 - no blank/zero 'unlimited'", () => {
    expect([parseCap("1"), parseCap("1000"), parseCap(" 5 ")]).toEqual([1, 1000, 5]);
    for (const bad of ["", "0", "-1", "1001", "2.5", "abc", null]) expect(parseCap(bad)).toBe(null);
  });

  it("says it changes only this service's current cap, and that the renewal cap is separate", () => {
    open();
    expect(screen.getByText((text) => text.includes(t("purchaseCap.scopeNote")))).toBeTruthy();
    expect(screen.getByText((text) => text.includes(t("purchaseCap.reservedNote", { value: 4 })))).toBeTruthy();
    expect(screen.getByTestId("cap-current").textContent).toBe("3");
  });

  it("refuses an invalid value before calling the server", async () => {
    const user = userEvent.setup();
    open();
    const input = screen.getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "0");
    expect(screen.getByRole("button", { name: t("purchaseCap.check") }).disabled).toBe(true);
    expect(api.previewPurchaseCap).not.toHaveBeenCalled();
  });

  it("raising: shows current, new, and that PPP's own cap no longer limits", async () => {
    const user = userEvent.setup();
    open();
    await previewWith(user, 5, shown());
    expect(api.previewPurchaseCap).toHaveBeenCalledWith(7, 5);
    expect(screen.getByText(t("purchaseCap.effectRaise", { cap: 5 }))).toBeTruthy();
    expect(screen.getByText(t("purchaseCap.pppNote"))).toBeTruthy();
  });

  it("lowering below the open sessions explains what is cut and what is not", async () => {
    const user = userEvent.setup();
    open();
    await previewWith(user, 1, shown({ new_value: 1, live_sessions_in_scope: 3, live_ppp_sessions: 3, excess_now: 2 }));
    const note = screen.getByRole("note").textContent;
    expect(note).toContain(t("purchaseCap.effectExcess", { live: 3, excess: 2, cap: 1 }));
    expect(note).toContain(t("purchaseCap.effectExcessPpp"));
    expect(note).not.toContain(t("purchaseCap.effectExcessOther"));   // a PPP-only service: no WireGuard talk
  });

  it("shows the panel's effective mode, as the server reported it", async () => {
    const user = userEvent.setup();
    open();
    await previewWith(user, 5, shown({ session_cap_mode: "enforced" }));
    expect(screen.getByTestId("cap-mode").textContent).toBe(t("purchaseCap.mode_enforced"));
  });

  it("not in force in this mode: says so, naming the mode", async () => {
    const user = userEvent.setup();
    open();
    await previewWith(user, 2, shown({ new_value: 2, in_force_now: false, session_cap_mode: "shadow" }));
    expect(screen.getByText(t("purchaseCap.effectNotInForce", { mode: "shadow" }))).toBeTruthy();
    expect(screen.getByTestId("cap-mode").textContent).toBe(t("purchaseCap.mode_shadow"));
  });

  it("mode unreadable: says no cap decision is taken now", async () => {
    const user = userEvent.setup();
    open();
    await previewWith(user, 2, shown({ new_value: 2, in_force_now: false, session_cap_mode: "unavailable" }));
    expect(screen.getByText(t("purchaseCap.effectModeUnavailable"))).toBeTruthy();
  });

  it("needs a reason, then changes exactly the previewed state once", async () => {
    const user = userEvent.setup();
    const onDone = open();
    const data = shown();
    await previewWith(user, 5, data);
    const apply = screen.getByRole("button", { name: t("purchaseCap.apply", { value: 5 }) });
    expect(apply.disabled).toBe(true);
    await user.type(screen.getByLabelText(t("purchaseCap.reason")), "customer bought 5 seats");
    api.changePurchaseCap.mockResolvedValue({ data: { after: 5 } });
    await user.dblClick(apply);
    expect(api.changePurchaseCap).toHaveBeenCalledTimes(1);
    expect(api.changePurchaseCap).toHaveBeenCalledWith(data, "customer bought 5 seats");
    expect(onDone).toHaveBeenCalled();
  });

  it("a stale preview is refused and must be checked again", async () => {
    const user = userEvent.setup();
    const onDone = open();
    await previewWith(user, 5, shown());
    await user.type(screen.getByLabelText(t("purchaseCap.reason")), "x");
    api.changePurchaseCap.mockRejectedValue(httpError(409, "purchase_cap_target_changed"));
    await user.click(screen.getByRole("button", { name: t("purchaseCap.apply", { value: 5 }) }));
    expect(await screen.findByText(t("purchaseCap.stale"))).toBeTruthy();
    expect(screen.getByRole("button", { name: t("purchaseCap.check") })).toBeTruthy();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("other work on the service blocks the change, in words", async () => {
    const user = userEvent.setup();
    open();
    await previewWith(user, 5, shown({ busy: "purchase_cap_approval_in_progress" }));
    expect(screen.getByText(t("purchaseCap.busyApproval"))).toBeTruthy();
    await user.type(screen.getByLabelText(t("purchaseCap.reason")), "x");
    expect(screen.getByRole("button", { name: t("purchaseCap.apply", { value: 5 }) }).disabled).toBe(true);
  });

  it("an unknown error code is not shown raw", async () => {
    const user = userEvent.setup();
    open();
    api.previewPurchaseCap.mockRejectedValue(httpError(500, "Traceback: secret"));
    const input = screen.getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "5");
    await user.click(screen.getByRole("button", { name: t("purchaseCap.check") }));
    expect(await screen.findByText(t("purchaseCap.failed"))).toBeTruthy();
    expect(screen.queryByText(/Traceback/)).toBe(null);
  });
});
