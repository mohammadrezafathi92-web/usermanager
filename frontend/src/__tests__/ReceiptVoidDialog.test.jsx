// Focused tests of the receipt-void UI (Receipt Void 19). Every API call is a
// mock; nothing leaves the process.
import React, { useState } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api/client.js", () => ({
  previewReceiptVoid: vi.fn(),
  executeReceiptVoid: vi.fn(),
  fetchReceiptVoidStatus: vi.fn(),
  retryReceiptVoid: vi.fn(),
  forceReceiptVoidStep: vi.fn(),
}));

import * as api from "../api/client.js";
import ReceiptVoidDialog, { VoidCell } from "../components/ReceiptVoidDialog.jsx";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import { translate } from "../i18n/translations.js";

const PREVIEW = {
  approval_uuid: "11111111-1111-1111-1111-111111111111", kind: "new", target_shape: "new_user",
  username: "scam1", telegram_id: 7101, amount: 1000, approval_mode: "auto", approved_by_telegram_id: null,
  resources: {
    delete: { kind: "user", id: 9 }, user_exists: true,
    purchases: [{ id: 4, package_name: "Shared", comment: null, status: "active", quota_bytes: 10 * 1024 ** 3,
                  used_bytes: 1024 ** 3, expire_at: null }],
    connections: [{ id: 7, protocol: "wireguard", node_id: 1, node_name: "mt-void", enabled: true }],
  },
  other_approvals_on_account: [{ approval_uuid: "22222222-2222-2222-2222-222222222222", kind: "renew",
                                 state: "completed", amount_snapshot: 1000 }],
  ledger: { voidable_sale_amount: 1000, reseller_credit_refund: null, discount: null,
            card: { event_id: 3, card_id: 1, amount: 1000 },
            entries: [{ id: 5, kind: "sale_new", amount: 1000, voided: false }] },
  wallet: { open_debt: 0, sources: [{ source_id: 8, origin_kind: "loyalty_reward", step: "loyalty_reconciliation",
            gross: 100, projected_clawback: 100, projected_debt: 0, projected_writeoff: 0,
            projected_settlement_reopen: 0, allocations: [{ allocation_id: 31, debit_id: 12, amount: 40 }] }] },
  loyalty: { purchase_events: [1], rewards: [{ id: 2, slot: 2, required_active_count: 4, caused_by_this_approval: false,
             credit_amount: 100, quota_bytes: 0 }] },
  effects: [{ id: 1, effect_type: "ledger_sale", effect_key: "sale", delta_value: 1000 }],
  blockers: [], preview_token: "token",
};
const ENTRY = { id: 5, kind: "sale_new", approval_uuid: PREVIEW.approval_uuid, approval_state: "completed" };
const status = (state, steps = []) => ({ data: { operation_id: 42, state, steps } });
const httpError = (code, detail) => Object.assign(new Error("http"), { response: { status: code, data: { detail } } });

function Harness({ lang = "fa", operationId = null }) {
  const [open, setOpen] = useState(false);
  try { localStorage.setItem("um_lang", lang); } catch { /* jsdom */ }
  return <LanguageProvider>
    <button type="button" onClick={() => setOpen(true)}>open-void</button>
    {open && <ReceiptVoidDialog entry={ENTRY} operationId={operationId} onClose={() => setOpen(false)} onDone={() => {}} />}
  </LanguageProvider>;
}

async function openDialog(user, props) {
  document.body.innerHTML = '<div id="root"></div>';
  const view = render(<Harness {...props} />, { container: document.getElementById("root") });
  await user.click(screen.getByText("open-void"));
  return view;
}

async function fill(user, lang = "fa") {
  const t = (key, vars) => translate(lang, key, vars);
  await user.type(await screen.findByLabelText(t("void.typeUsername", { username: "scam1" })), "scam1");
  await user.type(screen.getByLabelText(t("void.reason")), "fake receipt");
  await user.type(screen.getByLabelText(t("void.password")), "pw");
  await user.click(screen.getByLabelText(t("void.dangerAck")));
  return screen.getByRole("button", { name: t("void.execute") });
}

// A plain in-memory Storage: newer Node versions ship their own global
// localStorage that shadows jsdom's (and refuses without a backing file).
const memory = new Map();
vi.stubGlobal("localStorage", {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
  removeItem: (key) => memory.delete(key),
  clear: () => memory.clear(),
});

beforeEach(() => {
  vi.useRealTimers();
  api.previewReceiptVoid.mockResolvedValue({ data: PREVIEW });
});
afterEach(() => cleanup());

describe("receipt void dialog", () => {
  it("shows the full destructive preview: services, connections, other approvals, ledger, effects, allocations", async () => {
    const user = userEvent.setup();
    await openDialog(user, { lang: "en" });
    expect(await screen.findByText(/22222222-2222/)).toBeTruthy();
    expect(screen.getByText(/Renewal - Completed/)).toBeTruthy();
    expect(screen.getByText(/#4 Shared/)).toBeTruthy();
    expect(screen.getByText(/#7 wireguard - mt-void/)).toBeTruthy();
    expect(screen.getByText(/ledger_sale:sale/)).toBeTruthy();
    expect(screen.getByText(/Allocation #31 of debit #12/)).toBeTruthy();
    expect(screen.getByText(/Another healthy receipt's loyalty reward/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/private|psk|password":/i);
  });

  it("puts the focus on the typed-username field, traps Tab, and restores the opener on close", async () => {
    const user = userEvent.setup();
    await openDialog(user, { lang: "en" });
    const field = await screen.findByLabelText(/To confirm, type the username/);
    await waitFor(() => expect(document.activeElement).toBe(field));
    expect(document.getElementById("root").hasAttribute("inert")).toBe(true);
    const dialog = screen.getByRole("dialog");
    for (let i = 0; i < 30; i += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(screen.getByText("open-void"));
    expect(document.getElementById("root").hasAttribute("inert")).toBe(false);
  });

  it("disables execute on the first click, sends once, then polls to completion and can be closed", async () => {
    const user = userEvent.setup();
    let resolve;
    api.executeReceiptVoid.mockImplementation(() => new Promise((r) => { resolve = r; }));
    api.fetchReceiptVoidStatus.mockResolvedValueOnce(status("running", [{ step_key: "block", state: "pending" }]))
      .mockResolvedValue(status("completed", [{ step_key: "finalize", state: "done" }]));
    await openDialog(user, { lang: "en" });
    const button = await fill(user, "en");
    fireEvent.click(button);
    fireEvent.click(button);                          // the double click
    expect(api.executeReceiptVoid).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Running..." }).disabled).toBe(true);
    await act(async () => resolve({ data: { operation_id: 42, state: "running" } }));
    expect(await screen.findByText("The receipt was voided.", {}, { timeout: 5000 })).toBeTruthy();
    const close = screen.getByRole("button", { name: "Close", hidden: false });
    expect(close.disabled).toBe(false);               // busy cleared after success
    await user.click(close);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("offers retry and force on cleanup_required, each to its own endpoint", async () => {
    const user = userEvent.setup();
    api.fetchReceiptVoidStatus.mockResolvedValue(status("cleanup_required", [
      { step_key: "verify:7", state: "blocked", remote_outcome: "unverified", error_sanitized: "remote_unverified",
        result: { node_id: 1 } }]));
    api.retryReceiptVoid.mockResolvedValue({ data: { operation_id: 42, state: "cleanup_required" } });
    api.forceReceiptVoidStep.mockResolvedValue({ data: { operation_id: 42, state: "completed" } });
    await openDialog(user, { lang: "en", operationId: 42 });
    const heading = await screen.findByText(/Operation #42 - Cleanup required/);
    await waitFor(() => expect(document.activeElement).toBe(heading));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(api.retryReceiptVoid).toHaveBeenCalledWith(42);
    await user.click(await screen.findByRole("button", { name: "Abandon step" }));
    await user.type(screen.getAllByLabelText("Reason (required)")[0], "node destroyed");
    await user.type(screen.getAllByLabelText("Your own password")[0], "pw");
    await user.click(screen.getByLabelText(/I accept that this resource may remain/));
    await user.click(screen.getByRole("button", { name: "Abandon and continue" }));
    expect(api.forceReceiptVoidStep).toHaveBeenCalledWith(42, { step_key: "verify:7", reason: "node destroyed",
                                                                danger_ack: true }, "pw");
  });

  it.each([
    [403, "wrong", "Wrong password, or you are not allowed to do this."],
    [422, "typed_username_mismatch", "The typed username does not match the customer."],
    [409, "receipt_void_request_changed", "This receipt is already being voided with another request."],
    [500, "boom", "Server error. Nothing was confirmed; check the state again."],
  ])("shows a specific message for %s", async (code, detail, message) => {
    const user = userEvent.setup();
    api.executeReceiptVoid.mockRejectedValue(httpError(code, detail));
    await openDialog(user, { lang: "en" });
    await user.click(await fill(user, "en"));
    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Void it" })).toBeTruthy();   // busy cleared after the error
  });

  it("reloads the preview after 409 preview_expired", async () => {
    const user = userEvent.setup();
    api.executeReceiptVoid.mockRejectedValue(httpError(409, "preview_expired"));
    await openDialog(user, { lang: "en" });
    await user.click(await fill(user, "en"));
    expect(await screen.findByText(/a fresh preview was loaded/)).toBeTruthy();
    expect(api.previewReceiptVoid).toHaveBeenCalledTimes(2);
  });

  it("renders in Persian with the Persian strings", async () => {
    const user = userEvent.setup();
    await openDialog(user, { lang: "fa" });
    expect(await screen.findByRole("button", { name: translate("fa", "void.execute") })).toBeTruthy();
    expect(screen.getByText(translate("fa", "void.deleteAccount"))).toBeTruthy();
  });

  it("is a bottom sheet on phones and a centred dialog from sm: up", async () => {
    const user = userEvent.setup();
    await openDialog(user, { lang: "en" });
    const wrapper = screen.getByRole("dialog").parentElement;
    expect(wrapper.className).toMatch(/items-end/);
    expect(wrapper.className).toMatch(/sm:items-center/);
    expect(screen.getByRole("dialog").className).toMatch(/overflow-y-auto/);
  });
});

describe("void cell on a ledger row", () => {
  const t = (key) => translate("en", key);
  it("is enabled only on a traceable, completed sale/top-up and disabled 'not traceable' otherwise", () => {
    const onOpen = vi.fn();
    const { rerender, container } = render(<VoidCell entry={ENTRY} t={t} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "Void receipt" }));
    expect(onOpen).toHaveBeenCalledWith(ENTRY, null);
    rerender(<VoidCell entry={{ ...ENTRY, approval_uuid: null }} t={t} onOpen={onOpen} />);
    expect(screen.getByRole("button").disabled).toBe(true);
    expect(screen.getByRole("button").getAttribute("title")).toMatch(/Not traceable/);
    rerender(<VoidCell entry={{ ...ENTRY, kind: "expense" }} t={t} onOpen={onOpen} />);
    expect(container.textContent).toBe("");
    rerender(<VoidCell entry={{ ...ENTRY, approval_state: "cleanup_required", approval_void_operation_id: 42 }}
                       t={t} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "Cleanup required" }));
    expect(onOpen).toHaveBeenLastCalledWith(expect.anything(), 42);
  });
});
