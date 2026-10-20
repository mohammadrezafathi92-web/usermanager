import React, { useEffect, useId, useRef, useState } from "react";
import { Wallet, RefreshCw } from "lucide-react";
import {
  fetchWalletStatus, fetchWalletPreflight, fetchWalletReconcile, walletCutover, walletRollback,
  fetchApprovalRuntime, activateApprovalRuntime, deactivateApprovalRuntime, updateAutoApprovalRateMode,
} from "../api/client.js";

/**
 * Wallet generation (Receipt Void P9) and the joint activation (P9a) for the
 * superadmin: the read-only preflight and reconciliation, then the explicit
 * one-transaction steps - each asks for the admin's password and a second
 * confirmation. Nothing here runs on its own.
 */
export default function WalletCard({ t }) {
  const [status, setStatus] = useState(null);
  const [preflight, setPreflight] = useState(null);
  const [reconcile, setReconcile] = useState(null);
  const [runtime, setRuntime] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [autoMode, setAutoMode] = useState("off");
  const [saved, setSaved] = useState(false);
  const inFlight = useRef(false);
  const autoId = useId();

  const fail = (err) => {
    const detail = err?.response?.data?.detail;
    setError(typeof detail === "string" ? detail : detail?.error || detail?.code || t("wallet.error"));
  };

  const load = () => {
    setError("");
    return Promise.all([fetchWalletStatus(), fetchWalletPreflight(), fetchWalletReconcile(), fetchApprovalRuntime()])
      .then(([s, p, r, a]) => {
        setStatus(s.data); setPreflight(p.data); setReconcile(r.data); setRuntime(a.data);
        setAutoMode(a.data.requested_auto_rate_limit_mode || "off");
        return true;
      })
      .catch((err) => { fail(err); return false; });
  };

  useEffect(() => { load(); }, []);

  const act = (call, confirmKey) => {
    if (inFlight.current || !window.confirm(t(confirmKey))) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    call().then(load).catch(fail).finally(() => { inFlight.current = false; setBusy(false); });
  };

  const saveAutoMode = () => {
    if (!canEditAuto || inFlight.current || !window.confirm(t("wallet.autoConfirm", {
      value: t(`wallet.autoMode.${autoMode}`),
    }))) return;
    inFlight.current = true;
    setBusy(true); setError(""); setSaved(false);
    updateAutoApprovalRateMode(autoMode).then(async () => {
      const refreshed = await load();
      setSaved(refreshed);
    }).catch(fail).finally(() => { inFlight.current = false; setBusy(false); });
  };

  const phase = status?.phase;
  const required = runtime?.requested_registration_mode === "required";
  const canEditAuto = runtime?.effective_registration_mode === "required" && runtime?.key_identity_ready === true;
  const requestedAuto = runtime?.requested_auto_rate_limit_mode || "off";
  const effectiveAuto = runtime?.effective_auto_rate_limit_mode || "off";

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="w-7 h-7 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center shrink-0">
            <Wallet size={15} />
          </span>
          <h3 className="font-bold text-gray-700">{t("wallet.title")}</h3>
        </div>
        <button type="button" className="btn-secondary" disabled={busy} onClick={load}>
          <RefreshCw size={16} />
          {t("wallet.refresh")}
        </button>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("wallet.subtitle")}</p>
      {error && <div role="alert" className="text-sm text-red-600 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2 mb-4">{error}</div>}
      {status && (
        <div className="text-sm space-y-1 mb-4">
          <div>{t("wallet.phase", { value: phase })}</div>
          <div>{t("wallet.loyalty", { value: runtime?.loyalty_timing_mode || "-" })}</div>
          {preflight && phase === "normal" && (
            <div className={preflight.ready ? "text-emerald-600" : "text-amber-600"}>
              {preflight.ready ? t("wallet.preflightReady") : t("wallet.preflightBlocked", {
                value: preflight.blockers.map((b) => b.code).join("، "),
              })}
            </div>
          )}
          {reconcile && (
            <div className={reconcile.ok ? "text-emerald-600" : "text-red-500"}>
              {reconcile.ok ? t("wallet.reconcileOk") : t("wallet.reconcileProblems", { value: reconcile.count })}
            </div>
          )}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {phase === "normal" && (
          <button type="button" className="btn-primary" disabled={busy || !preflight?.ready}
            onClick={() => act(walletCutover, "wallet.cutoverConfirm")}>{t("wallet.cutover")}</button>
        )}
        {phase === "enforced" && !required && (
          <>
            <button type="button" className="btn-primary" disabled={busy}
              onClick={() => act(activateApprovalRuntime, "wallet.activateConfirm")}>{t("wallet.activate")}</button>
            <button type="button" className="btn-secondary" disabled={busy}
              onClick={() => act(walletRollback, "wallet.rollbackConfirm")}>{t("wallet.rollback")}</button>
          </>
        )}
        {required && (
          <button type="button" className="btn-secondary" disabled={busy}
            onClick={() => act(deactivateApprovalRuntime, "wallet.deactivateConfirm")}>{t("wallet.deactivate")}</button>
        )}
      </div>
      <section aria-labelledby={`${autoId}-title`} className="mt-5 border-t border-gray-200 dark:border-gray-700 pt-4 space-y-3 text-gray-800 dark:text-gray-100">
        <h4 id={`${autoId}-title`} className="font-bold text-gray-700 dark:text-gray-200">{t("wallet.autoTitle")}</h4>
        <p id={`${autoId}-help`} className="text-sm text-gray-600 dark:text-gray-300 leading-relaxed">{t("wallet.autoHelp")}</p>
        <p className="text-sm text-gray-600 dark:text-gray-300 leading-relaxed">{t("wallet.autoScope")}</p>
        <dl className="text-sm grid gap-2 sm:grid-cols-2" aria-live="polite">
          <div><dt className="text-gray-600 dark:text-gray-300">{t("wallet.autoRequested")}</dt><dd className="font-medium">{runtime ? t(`wallet.autoMode.${requestedAuto}`) : "—"}</dd></div>
          <div><dt className="text-gray-600 dark:text-gray-300">{t("wallet.autoEffective")}</dt><dd className="font-medium">{runtime ? t(`wallet.autoMode.${effectiveAuto}`) : "—"}</dd></div>
        </dl>
        {!canEditAuto && <p id={`${autoId}-blocked`} className="text-sm text-amber-700 dark:text-amber-300">{t("wallet.autoBlocked")}</p>}
        <div className="flex flex-wrap items-end gap-3" aria-busy={busy}>
          <div className="flex flex-col gap-1 w-full sm:w-auto sm:min-w-52">
            <label htmlFor={autoId} className="text-sm font-medium">{t("wallet.autoSelect")}</label>
            <select id={autoId} className="input min-h-11 disabled:opacity-60 disabled:cursor-not-allowed"
              aria-describedby={`${autoId}-help${!canEditAuto ? ` ${autoId}-blocked` : ""}`}
              disabled={busy || !canEditAuto} value={autoMode}
              onChange={(event) => { setAutoMode(event.target.value); setSaved(false); }}>
              {["off", "shadow", "enforced"].map((mode) => <option key={mode} value={mode}>{t(`wallet.autoMode.${mode}`)}</option>)}
            </select>
          </div>
          <button type="button" className="btn-primary min-h-11" disabled={busy || !canEditAuto || autoMode === requestedAuto}
            onClick={saveAutoMode}>{busy ? t("wallet.autoSaving") : t("wallet.autoSave")}</button>
        </div>
        {saved && <p role="status" className="text-sm text-emerald-700 dark:text-emerald-300">{t("wallet.autoSaved")}</p>}
      </section>
    </div>
  );
}
