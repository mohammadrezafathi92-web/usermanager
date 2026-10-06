import React, { useEffect, useState } from "react";
import Modal from "./Modal.jsx";
import { fetchUnpaidRefundPreview, cancelUnpaidPurchase } from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";
import { formatToman } from "../utils.js";

export default function UnpaidRefundDialog({ userId, purchase, onClose, onDone }) {
  const { t, language } = useLanguage();
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [pending, setPending] = useState(false);
  useEffect(() => {
    let active = true;
    setPreview(null); setError(""); setConfirmed(false); setResult(null); setPending(false);
    if (purchase) fetchUnpaidRefundPreview(userId, purchase.id)
      .then(({ data }) => { if (active) setPreview(data); })
      .catch(() => { if (active) setError(t("refund.unavailable")); });
    return () => { active = false; };
  }, [userId, purchase?.id, t]);

  async function execute() {
    setBusy(true); setError("");
    try {
      const { data } = await cancelUnpaidPurchase(userId, purchase.id);
      setResult(data); onDone();
    } catch (e) {
      const detail = e.response?.data?.detail;
      // A lost response can hide a completed operation. Retry the same purchase,
      // never a new payment or a plain delete: the server returns the same result.
      const uncertain = !e.response || detail?.code === "reseller_cancellation_pending" ||
        detail === "reseller_cancellation_pending";
      setPending(uncertain);
      setError(t(uncertain ? "refund.pending" : "refund.failed"));
    } finally { setBusy(false); }
  }
  const money = (value) => formatToman(value || 0, language);
  const percent = (value) => value == null ? t("refund.unlimited") : `${Number(value).toFixed(1)}٪`;
  const reasons = {
    reseller_cancellation_disabled: "refund.notEnabled",
    reseller_cancellation_platform_unsupported: "refund.platform",
    reseller_cancellation_ha_unsupported: "refund.ha",
    reseller_cancellation_protocol_unsupported: "refund.protocol",
    reseller_cancellation_rewards_untracked: "refund.rewards",
    reseller_receipt_requires_full_void: "refund.receipt",
    reseller_discount_requires_full_void: "refund.discount",
  };
  return <Modal open={!!purchase} onClose={() => { if (!busy) onClose(); }} title={t("refund.title")}>
    {result ? <div role="status" className="space-y-4">
      <p className="text-emerald-600 font-semibold">{t("refund.done", { amount: money(result.refund_amount) })}</p>
      <p className="text-sm text-gray-500">{t("refund.destination")}</p>
      <button type="button" className="btn-secondary w-full" onClick={onClose}>{t("common.close")}</button>
    </div> : <div className="space-y-4">
      <p className="text-sm">{t("refund.consequence")}</p>
      {preview && <dl className="grid grid-cols-2 gap-3 rounded-xl bg-gray-100 dark:bg-gray-800 p-4 text-sm">
        <dt>{t("refund.charged")}</dt><dd>{money(preview.charged_amount)}</dd>
        <dt>{t("refund.time")}</dt><dd>{percent(preview.time_percent)}</dd>
        <dt>{t("refund.quota")}</dt><dd>{percent(preview.quota_percent)}</dd>
        <dt>{t("refund.consumed")}</dt><dd>{percent(preview.consumed_percent)}</dd>
        <dt>{t("refund.estimate")}</dt><dd className="font-bold text-emerald-600">{money(preview.refund_amount)}</dd>
      </dl>}
      {!preview && !error && <p role="status">{t("refund.loading")}</p>}
      <p className="text-xs text-gray-500">{t("refund.formula")}</p>
      <p className="text-xs text-gray-500">{t("refund.destination")}</p>
      {preview && !preview.execution_available && <p className="text-sm text-amber-600">{t(reasons[preview.unavailable_reason] || "refund.unavailable")}</p>}
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)} />
        {t("refund.confirmUnpaid")}
      </label>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-danger flex-1" disabled={busy || !confirmed || (!pending && !preview?.execution_available)} onClick={execute}>
          {t(busy ? "refund.running" : pending ? "refund.retry" : "refund.execute")}
        </button>
        <button type="button" className="btn-secondary" disabled={busy} onClick={onClose}>{t("refund.keep")}</button>
      </div>
    </div>}
  </Modal>;
}
