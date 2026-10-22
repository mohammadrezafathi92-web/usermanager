import React, { useCallback, useEffect, useRef, useState } from "react";
import Modal from "./Modal.jsx";
import { fetchUnpaidRefundPreview, cancelUnpaidPurchase, fetchUnpaidCancellations } from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";
import { formatBytes, formatDateTime, formatToman } from "../utils.js";

// Every refusal or pending reason the server can give, in words. An unknown
// code falls back to the generic text instead of showing a raw identifier.
export const REFUND_REASONS = {
  reseller_cancellation_disabled: "refund.notEnabled",
  reseller_cancellation_platform_unsupported: "refund.platform",
  reseller_cancellation_ha_unsupported: "refund.ha",
  reseller_cancellation_protocol_unsupported: "refund.protocol",
  reseller_cancellation_rewards_untracked: "refund.rewards",
  reseller_receipt_requires_full_void: "refund.receipt",
  reseller_discount_requires_full_void: "refund.discount",
  reseller_sale_unproven: "refund.unproven",
  reseller_charge_unproven: "refund.unproven",
  reseller_sale_already_cancelled: "refund.alreadyCancelled",
  reseller_entitlement_changed: "refund.entitlementChanged",
  reseller_connections_changed: "refund.connectionsChanged",
  reseller_refund_account_missing: "refund.accountMissing",
  reseller_proration_unavailable: "refund.unmetered",
  reseller_paid_renewal_queued: "refund.renewalQueued",
  reseller_receipt_void_in_progress: "refund.voidInProgress",
  reseller_payment_pending: "refund.paymentPending",
  reseller_payment_queue_unreadable: "refund.paymentQueueUnreadable",
  reseller_operation_pending: "refund.operationPending",
  reseller_cancellation_busy: "refund.busy",
  reseller_cancellation_pending: "refund.otherPending",
  reseller_service_changed: "refund.serviceChanged",
  wg_usage_continuity_unverified: "refund.reasonReboot",
  wg_uptime_unverified: "refund.reasonReboot",
  wg_refund_identity_unverified: "refund.reasonIdentity",
  wg_refund_identity_conflict: "refund.reasonIdentity",
  refund_resource_changed: "refund.reasonIdentity",
  wg_refund_unreadable: "refund.reasonNode",
  node_or_database_unavailable: "refund.reasonNode",
  wg_refund_stop_failed: "refund.reasonStop",
  wg_refund_counters_not_stable: "refund.reasonCounters",
  wg_refund_counters_changed: "refund.reasonCounters",
  refund_counter_reset: "refund.reasonCounters",
  wg_refund_delete_unconfirmed: "refund.reasonDelete",
  refund_sessions_active: "refund.reasonSessions",
  ppp_accounting_stop_missing: "refund.reasonStopMissing",
  ppp_kick_unconfirmed: "refund.reasonKick",
  ppp_kick_failed: "refund.reasonKick",
  ppp_sessions_unreadable: "refund.reasonNode",
  ppp_session_reappeared: "refund.reasonKick",
  ppp_not_blocked: "refund.reasonKick",
  refund_usage_changed: "refund.reasonCounters",
};

const COMPONENT_STATE = { verified: "refund.compVerified", usage_captured: "refund.compCaptured",
  blocked: "refund.compBlocked", pending: "refund.compPending" };

function errorOf(e) {
  const detail = e?.response?.data?.detail;
  if (detail && typeof detail === "object") return { code: detail.code, reason: detail.reason };
  return { code: typeof detail === "string" ? detail : null, reason: null };
}

export default function UnpaidRefundDialog({ userId, purchase, onClose, onDone }) {
  const { t, language } = useLanguage();
  const [preview, setPreview] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState(null);
  const [message, setMessage] = useState(null);   // { tone, text }
  const sending = useRef(false);
  const purchaseId = purchase?.id;

  const reasonText = (code) => t(REFUND_REASONS[code] || "refund.unavailable");

  const purchaseKey = preview?.target?.purchase_key;
  const refresh = useCallback(async () => {
    // The server's record is the only truth after a lost or failed response.
    // A deleted service's id can be reused, so match its stable key first.
    const { data } = await fetchUnpaidCancellations(userId);
    const op = (data || []).find((o) => (purchaseKey && o.purchase_key
      ? o.purchase_key === purchaseKey : o.purchase_id === purchaseId)) || null;
    setOperation(op);
    return op;
  }, [userId, purchaseId, purchaseKey]);

  useEffect(() => {
    let active = true;
    setPreview(null); setLoadError(""); setConfirmed(false); setOperation(null); setMessage(null);
    if (purchaseId) fetchUnpaidRefundPreview(userId, purchaseId)
      .then(({ data }) => { if (active) { setPreview(data); setOperation(data.operation || null); } })
      .catch(() => { if (active) setLoadError(t("refund.loadFailed")); });
    return () => { active = false; };
  }, [userId, purchaseId, t]);

  async function execute() {
    if (sending.current) return;   // a double click sends once
    sending.current = true;
    setBusy(true); setMessage(null);
    try {
      const { data } = await cancelUnpaidPurchase(userId, purchaseId, purchaseKey);
      if (data?.state === "completed") {
        setOperation({ ...(operation || {}), state: "completed", refund_amount: data.refund_amount });
        onDone();
      } else {
        await refresh().catch(() => null);
        setMessage({ tone: "warn", text: t("refund.pending") });
      }
    } catch (e) {
      const { code, reason } = errorOf(e);
      // Possibly half done, or done with the answer lost (a retry of a
      // finished operation can even answer 404): read the record first.
      const op = await refresh().catch(() => null);
      if (op?.state === "completed") { onDone(); return; }
      if (!e?.response || code === "reseller_cancellation_pending" || op?.state === "pending") {
        setMessage({ tone: "warn", text: `${t("refund.pending")} ${reason ? reasonText(reason) : ""}`.trim() });
      } else {
        setMessage({ tone: "error", text: code ? reasonText(code) : t("refund.failed") });
      }
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  const money = (value) => formatToman(value || 0, language);
  const percent = (value) => value == null ? t("refund.unlimited") : `${Number(value).toFixed(1)}٪`;
  const target = preview?.target;
  const pending = operation?.state === "pending";
  const done = operation?.state === "completed";
  const canRun = confirmed && !busy && (pending || preview?.execution_available);

  return <Modal open={!!purchase} onClose={() => { if (!busy) onClose(); }} dismissible={!busy} title={t("refund.title")}>
    {done ? <div role="status" className="space-y-4">
      <p className="text-emerald-600 font-semibold">{t("refund.done", { amount: money(operation.refund_amount) })}</p>
      <p className="text-sm text-gray-500">{t("refund.destination")}</p>
      <button type="button" className="btn-secondary w-full" onClick={onClose}>{t("common.close")}</button>
    </div> : <div className="space-y-4">
      {preview && !pending && !preview.execution_available &&
        <p role="alert" className="text-sm rounded-lg border border-red-200 dark:border-red-900 text-red-700 dark:text-red-400 p-3">{reasonText(preview.unavailable_reason)}</p>}
      <p className="text-sm rounded-lg bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-300 p-3">{t("refund.notVoid")}</p>
      {target && <section aria-label={t("refund.targetTitle")} className="rounded-xl border border-gray-200 dark:border-gray-700 p-3 text-sm space-y-1">
        <h3 className="font-semibold">{t("refund.targetTitle")}</h3>
        <p>{target.package_name || t("refund.noPackage")} <span className="text-gray-500">#{target.purchase_id}</span>{target.comment ? ` · ${target.comment}` : ""}</p>
        <p className="text-gray-500">{t("refund.targetUsage", { used: formatBytes(target.used_bytes), quota: target.quota_bytes ? formatBytes(target.quota_bytes) : t("refund.unlimited") })}
          {target.expire_at ? ` · ${t("refund.targetExpiry", { date: formatDateTime(target.expire_at, language) })}` : ""}</p>
        <ul className="text-gray-500">{target.connections.map((c) => <li key={c.id}>{c.type} · {c.node || "—"}{c.name ? ` · ${c.name}` : ""}</li>)}</ul>
        <p className="text-gray-500">{target.other_services ? t("refund.untouched", { count: target.other_services }) : t("refund.untouchedNone")}</p>
      </section>}
      <p className="text-sm">{t("refund.consequence")}</p>
      {preview?.estimate_available && <dl className="grid grid-cols-2 gap-3 rounded-xl bg-gray-100 dark:bg-gray-800 p-4 text-sm">
        <dt>{t("refund.charged")}</dt><dd>{money(preview.charged_amount)}</dd>
        <dt>{t("refund.time")}</dt><dd>{percent(preview.time_percent)}</dd>
        <dt>{t("refund.quota")}</dt><dd>{percent(preview.quota_percent)}</dd>
        <dt>{t("refund.consumed")}</dt><dd>{percent(preview.consumed_percent)}</dd>
        <dt>{t("refund.estimate")}</dt><dd className="font-bold text-emerald-600">{money(preview.refund_amount)}</dd>
        {target?.charged_admin && <><dt>{t("refund.recipient")}</dt><dd>{target.charged_admin}</dd></>}
      </dl>}
      {!preview && !loadError && <p role="status">{t("refund.loading")}</p>}
      {loadError && <p role="alert" className="text-sm text-red-600">{loadError}</p>}
      <p className="text-xs text-gray-500">{t("refund.formula")}</p>
      <p className="text-xs text-gray-500">{t("refund.destination")}</p>
      {pending && <div role="status" className="rounded-lg border border-amber-300 p-3 text-sm space-y-1">
        <p className="font-semibold text-amber-700 dark:text-amber-400">{t("refund.pendingTitle")}</p>
        <p>{t("refund.progress", { stopped: operation.connections_stopped, removed: operation.connections_removed,
                                   total: operation.connections_total })}</p>
        {operation.components?.length > 0 && <ul className="space-y-1" aria-label={t("refund.components")}>
          {operation.components.map((c) => <li key={c.connection_id} className="flex flex-wrap gap-x-2">
            <span className="font-medium">{c.type}</span>
            <span>{t(COMPONENT_STATE[c.state] || "refund.compPending")}</span>
            {c.wait_reason && <span className="text-amber-700 dark:text-amber-400">
              {reasonText(c.wait_reason)}{c.wait_since ? ` (${t("refund.since", { date: formatDateTime(c.wait_since, language) })})` : ""}
            </span>}
          </li>)}
        </ul>}
        <p className="text-gray-500">{t("refund.pendingHelp")}</p>
      </div>}
      {(pending || preview?.execution_available) && <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)} />
        {t("refund.confirmUnpaid")}
      </label>}
      {message && <p role="alert" className={`text-sm ${message.tone === "error" ? "text-red-600" : "text-amber-600"}`}>{message.text}</p>}
      <div className="flex flex-wrap gap-2">
        {(pending || preview?.execution_available) &&
          <button type="button" className="btn-danger flex-1" disabled={!canRun} onClick={execute}>
            {t(busy ? "refund.running" : pending ? "refund.retry" : "refund.execute")}
          </button>}
        <button type="button" className="btn-secondary" disabled={busy} onClick={onClose}>{t(pending ? "common.close" : "refund.keep")}</button>
      </div>
    </div>}
  </Modal>;
}

// Shown on the customer page: an unfinished cancellation blocks every other
// change to this customer, so it must be visible after any reload.
export function UnpaidCancellationBanner({ userId, purchases, onResume, refreshKey }) {
  const { t, language } = useLanguage();
  const [ops, setOps] = useState([]);
  useEffect(() => {
    let active = true;
    fetchUnpaidCancellations(userId).then(({ data }) => { if (active) setOps(data || []); }).catch(() => {});
    return () => { active = false; };
  }, [userId, refreshKey]);
  const pending = ops.filter((o) => o.state === "pending");
  const recent = ops.filter((o) => o.state === "completed").slice(0, 3);
  if (!pending.length && !recent.length) return null;
  return <div className="space-y-2 mb-3">
    {pending.map((o) => {
      const purchase = (purchases || []).find((p) => p.id === o.purchase_id);
      return <div key={o.operation_id} role="status" className="rounded-xl border border-amber-300 bg-amber-50 dark:bg-amber-900/20 p-3 text-sm flex flex-wrap items-center gap-2">
        <span className="flex-1">{t("refund.bannerPending", { id: o.purchase_id, stopped: o.connections_stopped, total: o.connections_total })}</span>
        {purchase && <button type="button" className="btn-secondary" onClick={() => onResume(purchase)}>{t("refund.retry")}</button>}
      </div>;
    })}
    {recent.map((o) => <p key={o.operation_id} className="text-xs text-gray-500">
      {t("refund.bannerDone", { id: o.purchase_id, amount: formatToman(o.refund_amount || 0, language),
                                date: o.completed_at ? formatDateTime(o.completed_at, language) : "" })}
    </p>)}
  </div>;
}
