import React, { useEffect, useState } from "react";
import { Workflow, RefreshCw } from "lucide-react";
import {
  fetchProvisioningStatus, fetchProvisioningOperation, retryProvisioningOperation, forceProvisioningStep,
  fetchProvisioningOrphans, fetchApprovalRecoveries, decideApprovalRecovery,
} from "../api/client.js";
import { formatDateTime } from "../utils.js";

/**
 * Durable provisioning (routers/provisioning.py) for the superadmin: the
 * real p6_ready() and its blockers, operations stopped in cleanup_required
 * (retry = remove again safely; force = abandon one step with a reason),
 * receipt approvals whose execution key was lost (continue / cancel), and
 * the abandoned-resource audit. Every action asks for the admin's password
 * and a reason; nothing here runs on its own. No credential is ever shown -
 * the backend's step view is an allowlist.
 */
export default function ProvisioningCard({ t, language }) {
  const [status, setStatus] = useState(null);
  const [recoveries, setRecoveries] = useState([]);
  const [orphans, setOrphans] = useState([]);
  const [operationId, setOperationId] = useState("");
  const [operation, setOperation] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const fail = (err) => {
    const detail = err?.response?.data?.detail;
    setError(typeof detail === "string" ? detail : detail?.code || detail?.error || t("provisioning.error"));
  };

  const load = () => {
    setError("");
    Promise.all([fetchProvisioningStatus(), fetchApprovalRecoveries(), fetchProvisioningOrphans()])
      .then(([s, r, o]) => { setStatus(s.data); setRecoveries(r.data || []); setOrphans(o.data || []); })
      .catch(fail);
  };

  useEffect(load, []);

  const show = (id = operationId) => {
    if (!id) return;
    setError("");
    fetchProvisioningOperation(id).then((res) => setOperation(res.data)).catch(fail);
  };

  const act = (call) => {
    const reason = window.prompt(t("provisioning.reasonPrompt"));
    if (!reason || !reason.trim()) return;
    setBusy(true);
    setError("");
    call(reason.trim())
      .then(() => { load(); if (operation) show(operation.id); })
      .catch(fail)
      .finally(() => setBusy(false));
  };

  const cleanupCount = status?.operation_counts?.cleanup_required || 0;

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="w-7 h-7 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center shrink-0">
            <Workflow size={15} />
          </span>
          <h3 className="font-bold text-gray-700">{t("provisioning.title")}</h3>
        </div>
        <button type="button" className="btn-secondary" onClick={load}>
          <RefreshCw size={16} />
          {t("provisioning.refresh")}
        </button>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("provisioning.subtitle")}</p>

      {error && <div className="text-sm text-red-500 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2 mb-4">{error}</div>}

      {status && (
        <div className="text-sm mb-4 space-y-1">
          <div className={status.p6_ready ? "text-emerald-600" : "text-amber-600"}>
            {status.p6_ready ? t("provisioning.ready") : t("provisioning.notReady")}
            {status.runtime && <span className="text-gray-400"> · gate: {status.runtime.gate_mode}</span>}
          </div>
          {status.blockers?.length > 0 && (
            <div className="text-xs text-gray-500">{t("provisioning.blockers")}: {status.blockers.join("، ")}</div>
          )}
          <div className="text-xs text-gray-500">{t("provisioning.cleanup")}: {cleanupCount}</div>
        </div>
      )}

      <div className="mb-4">
        <h4 className="text-sm font-semibold text-gray-600 mb-2">{t("provisioning.recoveries")}</h4>
        {recoveries.length === 0 && <div className="text-xs text-gray-400">{t("provisioning.none")}</div>}
        {recoveries.map((item) => (
          <div key={item.approval_uuid} className="flex items-center justify-between gap-2 text-xs py-1 border-b border-gray-100 dark:border-slate-800">
            <span className="font-mono truncate">
              {item.approval_uuid.slice(0, 8)} · {item.operation_state || "-"} · {item.error_code || ""}
              {item.stale && <span className="text-amber-600"> · {t("provisioning.stale")}</span>}
            </span>
            <span className="flex gap-1 shrink-0">
              {item.continue_allowed && (
                <button type="button" className="btn-secondary" disabled={busy}
                  onClick={() => act((reason) => decideApprovalRecovery(item.approval_uuid, "continue", reason))}>
                  {t("provisioning.continue")}
                </button>
              )}
              <button type="button" className="btn-secondary" disabled={busy}
                onClick={() => act((reason) => decideApprovalRecovery(item.approval_uuid, "cancel", reason))}>
                {t("provisioning.cancel")}
              </button>
            </span>
          </div>
        ))}
      </div>

      <div className="mb-4">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-sm">{t("provisioning.lookup")}</span>
          <input className="input w-28" inputMode="numeric" value={operationId}
            onChange={(e) => setOperationId(e.target.value.replace(/[^0-9]/g, ""))} />
          <button type="button" className="btn-secondary" onClick={() => show()}>{t("provisioning.show")}</button>
        </div>
        {operation && (
          <div className="text-xs space-y-1">
            <div>#{operation.id} · {operation.operation_type} · {operation.state} {operation.error_code ? `· ${operation.error_code}` : ""}</div>
            {operation.steps.map((step) => (
              <div key={step.id} className="flex items-center justify-between gap-2">
                <span className="font-mono">step {step.id} · node {step.node_id} · {step.backend} · {step.state}</span>
                {operation.state === "cleanup_required" && step.state === "cleanup_required" && (
                  <button type="button" className="btn-secondary" disabled={busy}
                    onClick={() => act((reason) => forceProvisioningStep(operation.id, step.id, reason))}>
                    {t("provisioning.force")}
                  </button>
                )}
              </div>
            ))}
            {operation.state === "cleanup_required" && (
              <button type="button" className="btn-primary" disabled={busy}
                onClick={() => act((reason) => retryProvisioningOperation(operation.id, reason))}>
                {t("provisioning.retry")}
              </button>
            )}
          </div>
        )}
      </div>

      <div>
        <h4 className="text-sm font-semibold text-gray-600 mb-2">{t("provisioning.orphans")}</h4>
        {orphans.length === 0 && <div className="text-xs text-gray-400">{t("provisioning.none")}</div>}
        {orphans.map((item) => (
          <div key={item.step_id} className="text-xs py-1 border-b border-gray-100 dark:border-slate-800">
            #{item.operation_id}/{item.step_id} · node {item.node_id} · {item.backend} ·{" "}
            {item.wg_peer_name || item.account_username || item.xr_email || "-"} · {item.force_reason}
            {item.forced_at && <span className="text-gray-400"> · {formatDateTime(item.forced_at, language)}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
