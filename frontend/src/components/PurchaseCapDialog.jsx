import React, { useEffect, useRef, useState } from "react";
import Modal from "./Modal.jsx";
import { previewPurchaseCap, changePurchaseCap } from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";

// Changing the concurrent-session cap of ONE purchased service (superadmin).
// The server previews the exact state; the change is a compare-and-set
// against that preview, so nothing written here can overwrite a newer value.
// Only the cap in force: the queued renewal's own cap is a separate field.
export const CAP_REASONS = {
  purchase_cap_value_invalid: "purchaseCap.invalid",
  purchase_cap_preview_stale: "purchaseCap.stale",
  purchase_cap_target_changed: "purchaseCap.stale",
  purchase_cap_approval_in_progress: "purchaseCap.busyApproval",
  purchase_cap_operation_in_progress: "purchaseCap.busyOperation",
  purchase_cap_void_in_progress: "purchaseCap.busyVoid",
  purchase_cap_cancellation_pending: "purchaseCap.busyCancellation",
  purchase_not_found: "purchaseCap.notFound",
  purchase_cap_reason_required: "purchaseCap.reasonRequired",
};

const MAX_CAP = 1000;

export function parseCap(text) {
  const value = String(text ?? "").trim();
  if (!/^\d+$/.test(value)) return null;
  const n = Number(value);
  return n >= 1 && n <= MAX_CAP ? n : null;
}

function detailOf(e) {
  const detail = e?.response?.data?.detail;
  return typeof detail === "string" ? detail : null;
}

export default function PurchaseCapDialog({ purchase, onClose, onDone }) {
  const { t } = useLanguage();
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [shown, setShown] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sending = useRef(false);
  const current = purchase?.max_concurrent_sessions || null;

  useEffect(() => {
    setValue(current ? String(current) : ""); setReason(""); setShown(null); setError(""); setBusy(false);
    sending.current = false;
  }, [purchase?.id, current]);

  const reasonText = (code) => t(CAP_REASONS[code] || "purchaseCap.failed");
  const parsed = parseCap(value);

  async function check() {
    if (parsed === null) { setError(t("purchaseCap.invalid")); return; }
    setBusy(true); setError(""); setShown(null);
    try {
      const { data } = await previewPurchaseCap(purchase.id, parsed);
      setShown(data);
    } catch (e) {
      setError(reasonText(detailOf(e)));
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (sending.current || !shown) return;
    sending.current = true;
    setBusy(true); setError("");
    try {
      await changePurchaseCap(shown, reason.trim());
      onDone();             // stays latched: a done change is never sent again from this preview
    } catch (e) {
      const code = detailOf(e);
      setError(reasonText(code));
      if (code === "purchase_cap_preview_stale" || code === "purchase_cap_target_changed") setShown(null);
      sending.current = false;
    } finally {
      setBusy(false);
    }
  }

  const before = (v) => (v ? String(v) : t("purchaseCap.notSet"));
  const lowering = shown && shown.current_value && shown.new_value < shown.current_value;
  let effect = null;
  if (shown) {
    if (shown.session_cap_mode === "unavailable") effect = t("purchaseCap.effectModeUnavailable");
    else if (!shown.in_force_now) effect = t("purchaseCap.effectNotInForce", { mode: shown.session_cap_mode });
    else if (shown.excess_now > 0) effect = [
      t("purchaseCap.effectExcess", { live: shown.live_sessions_in_scope, excess: shown.excess_now, cap: shown.new_value }),
      shown.live_other_sessions > 0 ? t("purchaseCap.effectExcessOther") : null,
      shown.live_ppp_sessions > 0 ? t("purchaseCap.effectExcessPpp") : null,
    ].filter(Boolean).join(" ");
    else if (lowering) effect = t("purchaseCap.effectLowerNoExcess", { live: shown.live_sessions_in_scope });
    else effect = t("purchaseCap.effectRaise", { cap: shown.new_value });
  }
  const pppOwnCaps = shown?.scope?.some((c) => ["openvpn", "l2tp", "ikev2", "sstp"].includes(c.type));

  return <Modal open={!!purchase} onClose={() => { if (!busy) onClose(); }} dismissible={!busy} title={t("purchaseCap.title")}>
    <div className="space-y-4">
      <p className="text-sm rounded-lg bg-sky-50 dark:bg-sky-900/20 text-sky-800 dark:text-sky-300 p-3">
        {t("purchaseCap.scopeNote")}
        {purchase?.reserved_max_concurrent_sessions
          ? ` ${t("purchaseCap.reservedNote", { value: purchase.reserved_max_concurrent_sessions })}` : ""}
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <span className="label">{t("purchaseCap.current")}</span>
          <p className="input bg-gray-50 dark:bg-gray-800" data-testid="cap-current">{before(current)}</p>
        </div>
        <label className="block">
          <span className="label">{t("purchaseCap.newValue")}</span>
          <input className="input w-full" type="number" inputMode="numeric" min={1} max={MAX_CAP} step={1}
                 data-autofocus value={value} disabled={busy}
                 onChange={(e) => { setValue(e.target.value); setShown(null); setError(""); }} />
        </label>
      </div>
      <p className="hint">{t("purchaseCap.valueHint", { max: MAX_CAP })}</p>
      {!shown && <button type="button" className="btn-secondary w-full" disabled={busy || parsed === null} onClick={check}>
        {t("purchaseCap.check")}
      </button>}
      {shown && <section aria-label={t("purchaseCap.summary")} className="rounded-xl border border-gray-200 dark:border-gray-700 p-3 text-sm space-y-2">
        <dl className="grid grid-cols-2 gap-2">
          <dt className="text-gray-500">{t("purchaseCap.current")}</dt><dd>{before(shown.current_value)}</dd>
          <dt className="text-gray-500">{t("purchaseCap.newValue")}</dt><dd className="font-semibold">{shown.new_value}</dd>
          <dt className="text-gray-500">{t("purchaseCap.liveNow")}</dt><dd>{shown.live_sessions_in_scope}</dd>
          <dt className="text-gray-500">{t("purchaseCap.mode")}</dt>
          <dd data-testid="cap-mode">{t(`purchaseCap.mode_${shown.session_cap_mode}`)}</dd>
        </dl>
        <p role="note" className={shown.excess_now > 0 && shown.in_force_now
          ? "rounded-lg bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-300 p-2" : "text-gray-600 dark:text-gray-300"}>
          {effect}
        </p>
        {pppOwnCaps && shown.in_force_now && <p className="hint">{t("purchaseCap.pppNote")}</p>}
        {shown.busy && <p role="alert" className="text-red-600">{reasonText(shown.busy)}</p>}
        <label className="block">
          <span className="label">{t("purchaseCap.reason")}</span>
          <input className="input w-full" maxLength={500} value={reason} disabled={busy}
                 onChange={(e) => setReason(e.target.value)} />
        </label>
        <button type="button" className="btn-primary w-full" onClick={apply}
                disabled={busy || !!shown.busy || !reason.trim()}>
          {t("purchaseCap.apply", { value: shown.new_value })}
        </button>
      </section>}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    </div>
  </Modal>;
}
