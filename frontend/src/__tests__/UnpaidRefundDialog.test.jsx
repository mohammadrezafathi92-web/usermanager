// Unpaid-service cancellation UI. Every API call is a mock.
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api/client.js", () => ({
  fetchUnpaidRefundPreview: vi.fn(),
  cancelUnpaidPurchase: vi.fn(),
  fetchUnpaidCancellations: vi.fn(),
}));

import * as api from "../api/client.js";
import UnpaidRefundDialog, { UnpaidCancellationBanner } from "../components/UnpaidRefundDialog.jsx";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import { translate } from "../i18n/translations.js";

const t = (key, vars) => translate("fa", key, vars);
const TARGET = { purchase_id: 7, package_name: "ماهانه", comment: "علی", created_at: "2026-10-01T00:00:00",
  expire_at: "2026-11-01T00:00:00", quota_bytes: 10 * 1024 ** 3, used_bytes: 1024 ** 3,
  connections: [{ id: 3, type: "wireguard", node: "test-node", name: "peer" }], other_services: 2,
  charged_admin: "seller1" };
const PREVIEW = { target: TARGET, operation: null, estimate_available: true, charged_amount: 100000,
  refund_amount: 75000, time_percent: 20, quota_percent: 30, consumed_percent: 25,
  execution_available: true, unavailable_reason: null };
const PENDING = { operation_id: 9, purchase_id: 7, state: "pending", connections_total: 1,
  connections_stopped: 1, connections_removed: 0, refund_amount: null };
const httpError = (status, detail) => Object.assign(new Error("http"), { response: { status, data: { detail } } });

const memory = new Map();
vi.stubGlobal("localStorage", {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
  removeItem: (key) => memory.delete(key),
  clear: () => memory.clear(),
});

function open(onDone = () => {}) {
  document.body.innerHTML = '<div id="root"></div>';
  return render(<LanguageProvider>
    <UnpaidRefundDialog userId={1} purchase={{ id: 7 }} onClose={() => {}} onDone={onDone} />
  </LanguageProvider>, { container: document.getElementById("root") });
}

async function confirmAndRun(user) {
  await user.click(await screen.findByLabelText(t("refund.confirmUnpaid")));
  return screen.getByRole("button", { name: t("refund.execute") });
}

beforeEach(() => {
  vi.resetAllMocks();
  api.fetchUnpaidRefundPreview.mockResolvedValue({ data: PREVIEW });
  api.fetchUnpaidCancellations.mockResolvedValue({ data: [] });
});
afterEach(() => cleanup());

describe("unpaid cancellation dialog", () => {
  it("previews exactly the target service, what it leaves alone, and that it is not a receipt void", async () => {
    open();
    expect(await screen.findByText(t("refund.targetTitle"))).toBeTruthy();
    expect(screen.getByText(/ماهانه/)).toBeTruthy();
    expect(screen.getByText(/test-node/)).toBeTruthy();
    expect(screen.getByText(t("refund.untouched", { count: 2 }))).toBeTruthy();
    expect(screen.getByText(t("refund.notVoid"))).toBeTruthy();
    expect(screen.getByText("seller1")).toBeTruthy();
  });

  it("explains a refusal in words and offers no execute button", async () => {
    api.fetchUnpaidRefundPreview.mockResolvedValue({ data: { ...PREVIEW, execution_available: false,
      unavailable_reason: "reseller_payment_pending" } });
    open();
    expect(await screen.findByText(t("refund.paymentPending"))).toBeTruthy();
    expect(screen.queryByRole("button", { name: t("refund.execute") })).toBeNull();
  });

  it("an ineligible sale still shows its target, without a made-up estimate", async () => {
    api.fetchUnpaidRefundPreview.mockResolvedValue({ data: { target: TARGET, operation: null,
      estimate_available: false, execution_available: false, unavailable_reason: "reseller_charge_unproven" } });
    open();
    expect(await screen.findByText(t("refund.unproven"))).toBeTruthy();
    expect(screen.getByText(t("refund.targetTitle"))).toBeTruthy();
    expect(screen.queryByText(t("refund.estimate"))).toBeNull();
  });

  it("a double click sends one request; success only on a completed state", async () => {
    let resolve;
    api.cancelUnpaidPurchase.mockReturnValue(new Promise((r) => { resolve = r; }));
    const onDone = vi.fn();
    const user = userEvent.setup();
    open(onDone);
    const button = await confirmAndRun(user);
    await user.dblClick(button);
    expect(api.cancelUnpaidPurchase).toHaveBeenCalledTimes(1);
    resolve({ data: { state: "completed", refund_amount: 75000, operation_id: 9 } });
    expect(await screen.findByText(/به اعتبار فروشنده برگشت/)).toBeTruthy();
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("a lost response reads the server record: done if it completed", async () => {
    api.cancelUnpaidPurchase.mockRejectedValue(new Error("Network Error"));
    api.fetchUnpaidCancellations.mockResolvedValue({ data: [{ ...PENDING, state: "completed", refund_amount: 75000,
                                                              connections_removed: 1 }] });
    const onDone = vi.fn();
    const user = userEvent.setup();
    open(onDone);
    await user.click(await confirmAndRun(user));
    expect(await screen.findByText(/به اعتبار فروشنده برگشت/)).toBeTruthy();
    expect(onDone).toHaveBeenCalled();
  });

  it("a failed remote delete shows the pending operation, its progress and reason, and resumes it", async () => {
    api.cancelUnpaidPurchase.mockRejectedValueOnce(httpError(409, { code: "reseller_cancellation_pending",
      operation_id: 9, reason: "wg_refund_delete_unconfirmed" }));
    api.fetchUnpaidCancellations.mockResolvedValue({ data: [PENDING] });
    const user = userEvent.setup();
    open();
    await user.click(await confirmAndRun(user));
    expect(await screen.findByText(t("refund.pendingTitle"))).toBeTruthy();
    expect(screen.getByText(new RegExp(t("refund.reasonDelete")))).toBeTruthy();
    expect(screen.queryByText(/به اعتبار فروشنده برگشت/)).toBeNull();
    api.cancelUnpaidPurchase.mockResolvedValueOnce({ data: { state: "completed", refund_amount: 75000 } });
    await user.click(screen.getByRole("button", { name: t("refund.retry") }));
    expect(await screen.findByText(/به اعتبار فروشنده برگشت/)).toBeTruthy();
    expect(api.cancelUnpaidPurchase).toHaveBeenCalledTimes(2);
  });

  it("an operation left pending before a reload is shown with a resume button", async () => {
    api.fetchUnpaidRefundPreview.mockResolvedValue({ data: { ...PREVIEW, operation: PENDING, execution_available: true } });
    const user = userEvent.setup();
    open();
    expect(await screen.findByText(t("refund.pendingTitle"))).toBeTruthy();
    await user.click(screen.getByLabelText(t("refund.confirmUnpaid")));
    expect(screen.getByRole("button", { name: t("refund.retry") }).disabled).toBe(false);
  });

  it("a mixed service shows each component and what it waits for, with no refund", async () => {
    const op = { ...PENDING, connections_total: 2, connections_stopped: 1, components: [
      { connection_id: 1, type: "wireguard", state: "usage_captured", wait_reason: null, wait_since: null },
      { connection_id: 2, type: "l2tp", state: "blocked", wait_reason: "ppp_accounting_stop_missing",
        wait_since: "2026-10-23T10:00:00" }] };
    api.fetchUnpaidRefundPreview.mockResolvedValue({ data: { ...PREVIEW, operation: op, execution_available: true } });
    open();
    const list = await screen.findByRole("list", { name: t("refund.components") });
    expect(list.textContent).toContain(t("refund.compCaptured"));
    expect(list.textContent).toContain(t("refund.compBlocked"));
    expect(list.textContent).toContain(t("refund.reasonStopMissing"));
    expect(screen.queryByText((text) => text.includes(t("refund.done", { amount: "" }).trim()))).toBe(null);
  });

  it("an answer for a finished operation is matched by its service key, not a reused id", async () => {
    api.fetchUnpaidRefundPreview.mockResolvedValue({ data: { ...PREVIEW, target: { ...TARGET, purchase_key: "new-key" } } });
    api.cancelUnpaidPurchase.mockRejectedValue(new Error("Network Error"));
    api.fetchUnpaidCancellations.mockResolvedValue({ data: [{ ...PENDING, state: "completed", refund_amount: 5,
                                                              purchase_key: "old-key" }] });
    const user = userEvent.setup();
    open();
    await user.click(await confirmAndRun(user));
    expect(await screen.findByText(new RegExp(t("refund.pending").slice(0, 20)))).toBeTruthy();
    expect(screen.queryByText(/به اعتبار فروشنده برگشت/)).toBeNull();
    expect(api.cancelUnpaidPurchase).toHaveBeenCalledWith(1, 7, "new-key");
  });

  it("a busy refusal says nothing changed", async () => {
    api.cancelUnpaidPurchase.mockRejectedValue(httpError(503, "reseller_cancellation_busy"));
    const user = userEvent.setup();
    open();
    await user.click(await confirmAndRun(user));
    expect(await screen.findByText(t("refund.busy"))).toBeTruthy();
  });
});

describe("customer page banner", () => {
  it("shows an unfinished cancellation after a reload and opens it for resuming", async () => {
    api.fetchUnpaidCancellations.mockResolvedValue({ data: [PENDING] });
    const onResume = vi.fn();
    const user = userEvent.setup();
    render(<LanguageProvider><UnpaidCancellationBanner userId={1} purchases={[{ id: 7 }]} onResume={onResume} /></LanguageProvider>);
    expect(await screen.findByText(t("refund.bannerPending", { id: 7, stopped: 1, total: 1 }))).toBeTruthy();
    await user.click(screen.getByRole("button", { name: t("refund.retry") }));
    expect(onResume).toHaveBeenCalledWith({ id: 7 });
  });
});
