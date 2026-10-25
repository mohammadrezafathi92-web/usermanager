import React, { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw, ShieldCheck } from "lucide-react";
import { claimProvisioningOwnership, fetchProvisioningStatus, reclaimProvisioningOwnership } from "../api/client.js";

export default function ProvisioningOwnershipCard({ t }) {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmedStopped, setConfirmedStopped] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetchProvisioningStatus();
      setStatus(response.data);
    } catch (err) {
      const detail = err?.response?.data?.detail;
      setError(typeof detail === "string" ? detail : detail?.code || t("provisioning.error"));
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { load(); }, [load]);

  const runtime = status?.runtime;
  const action = runtime?.ownership_action;
  const reclaim = action === "reclaim";
  const claim = action === "claim";
  const otherHost = action === "foreign_owner";
  const identityUnavailable = action === "host_identity_unavailable";
  const active = runtime?.owner_state === "active" && action === null;

  const submit = async (event) => {
    event.preventDefault();
    if (busy || (reclaim && (!confirmedStopped || !reason.trim()))) return;
    setBusy(true);
    setError("");
    try {
      if (reclaim) await reclaimProvisioningOwnership(reason.trim());
      else if (claim) await claimProvisioningOwnership();
      setFormOpen(false);
      setReason("");
      setConfirmedStopped(false);
      await load();
    } catch (err) {
      const detail = err?.response?.data?.detail;
      setError(typeof detail === "string" ? detail : detail?.code || t("provisioning.error"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card mb-4" aria-labelledby="provisioning-owner-title">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-2 min-w-0">
          <span className="w-8 h-8 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center shrink-0">
            <ShieldCheck size={17} />
          </span>
          <div className="min-w-0">
            <h2 id="provisioning-owner-title" className="font-bold text-gray-800 dark:text-gray-100">
              {t("provisioning.ownerTitle")}
            </h2>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {t("provisioning.ownerHelp")}
            </p>
          </div>
        </div>
        <button type="button" className="btn-secondary shrink-0" onClick={load} disabled={loading || busy}>
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
          {t("provisioning.refresh")}
        </button>
      </div>

      {error && <div role="status" aria-live="polite" className="mt-3 rounded-lg bg-red-50 dark:bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-300 break-words">{error}</div>}
      {!status && !error && <p role="status" className="mt-3 text-sm text-gray-500">{t("common.loading")}</p>}
      {status && !runtime && <p role="status" aria-live="polite" className="mt-3 text-sm text-amber-700 dark:text-amber-300">{t("provisioning.ownerStatusUnavailable")}</p>}

      {runtime && (
        <div className="mt-3 space-y-2">
          <div role="status" className={`flex items-start gap-2 text-sm ${active ? "text-emerald-700 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}`}>
            {active ? <CheckCircle2 size={16} className="mt-0.5 shrink-0" /> : <AlertTriangle size={16} className="mt-0.5 shrink-0" />}
            <span>{active ? t("provisioning.ownerActive") : action ? t(`provisioning.ownerAction.${action}`) : t("provisioning.ownerStatusUnavailable")}</span>
          </div>

          {(reclaim || claim) && !formOpen && (
            <button type="button" className="btn-primary min-h-[44px]" disabled={busy || loading} onClick={() => setFormOpen(true)}>
              {t(reclaim ? "provisioning.ownerReclaim" : "provisioning.ownerClaim")}
            </button>
          )}

          {formOpen && (reclaim || claim) && (
            <form onSubmit={submit} className="rounded-xl border border-amber-200 dark:border-amber-700/60 bg-amber-50/70 dark:bg-amber-500/5 p-3 space-y-3">
              {reclaim && (
                <>
                  <p className="text-sm text-amber-900 dark:text-amber-200">{t("provisioning.ownerReclaimWarning")}</p>
                  <label className="block">
                    <span className="block text-xs text-gray-600 dark:text-gray-300 mb-1">{t("provisioning.ownerReason")}</span>
                    <input className="input w-full" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} required />
                  </label>
                  <label className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-200">
                    <input type="checkbox" className="mt-1" checked={confirmedStopped} onChange={(event) => setConfirmedStopped(event.target.checked)} />
                    <span>{t("provisioning.ownerPreviousStopped")}</span>
                  </label>
                </>
              )}
              {claim && <p className="text-sm text-gray-700 dark:text-gray-200">{t("provisioning.ownerClaimWarning")}</p>}
              <p className="text-xs text-gray-500 dark:text-gray-400">{t("provisioning.ownerPasswordNote")}</p>
              <div className="flex flex-wrap gap-2">
                <button type="button" className="btn-secondary min-h-[44px]" disabled={busy} onClick={() => { setFormOpen(false); setReason(""); setConfirmedStopped(false); }}>
                  {t("common.cancel")}
                </button>
                <button type="submit" className="btn-primary min-h-[44px]" disabled={busy || (reclaim && (!confirmedStopped || !reason.trim()))}>
                  {busy ? t("provisioning.ownerWorking") : t(reclaim ? "provisioning.ownerReclaim" : "provisioning.ownerClaim")}
                </button>
              </div>
            </form>
          )}

          {otherHost && <p className="text-xs text-gray-500 dark:text-gray-400">{t("provisioning.ownerForeignHelp")}</p>}
          {identityUnavailable && <p className="text-xs text-gray-500 dark:text-gray-400">{t("provisioning.ownerIdentityHelp")}</p>}
        </div>
      )}
    </section>
  );
}
