import React, { useEffect, useState } from "react";
import { Wallet, RefreshCw } from "lucide-react";
import {
  fetchWalletStatus, fetchWalletPreflight, fetchWalletReconcile, walletCutover, walletRollback,
  fetchApprovalRuntime, activateApprovalRuntime, deactivateApprovalRuntime,
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

  const fail = (err) => {
    const detail = err?.response?.data?.detail;
    setError(typeof detail === "string" ? detail : detail?.error || detail?.code || t("wallet.error"));
  };

  const load = () => {
    setError("");
    Promise.all([fetchWalletStatus(), fetchWalletPreflight(), fetchWalletReconcile(), fetchApprovalRuntime()])
      .then(([s, p, r, a]) => { setStatus(s.data); setPreflight(p.data); setReconcile(r.data); setRuntime(a.data); })
      .catch(fail);
  };

  useEffect(load, []);

  const act = (call, confirmKey) => {
    if (!window.confirm(t(confirmKey))) return;
    setBusy(true);
    setError("");
    call().then(load).catch(fail).finally(() => setBusy(false));
  };

  const phase = status?.phase;
  const required = runtime?.requested_registration_mode === "required";

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="w-7 h-7 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center shrink-0">
            <Wallet size={15} />
          </span>
          <h3 className="font-bold text-gray-700">{t("wallet.title")}</h3>
        </div>
        <button type="button" className="btn-secondary" onClick={load}>
          <RefreshCw size={16} />
          {t("wallet.refresh")}
        </button>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("wallet.subtitle")}</p>
      {error && <div className="text-sm text-red-500 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2 mb-4">{error}</div>}
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
    </div>
  );
}
