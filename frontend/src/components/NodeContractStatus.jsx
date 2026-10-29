import React from "react";
import { useLanguage } from "../context/LanguageContext.jsx";

const states = new Set(["ready", "not_checked", "stale", "verifying", "failed", "cleanup_unknown", "gate_not_enforced", "unavailable", "unsupported"]);
const reasons = {
  config_changed: "config", node_config_changed_during_probe: "config",
  adapter_version_changed: "version", backend_changed: "config",
  wireguard_interface_missing: "interface", server_version_unreadable: "server",
  ownership_or_lock_unavailable: "lock", schema_not_ready: "schema",
  read_before_not_absent: "identity", read_after_create_not_match: "identity",
  read_before_not_a_complete_answer: "response", read_not_exist_unstable: "response",
  delete_not_exist_unstable: "response", read_failed: "response",
  delete_unverified: "cleanup", read_after_delete_not_absent: "cleanup",
  probe_cleanup_verified_absent: "cleanupVerifiedAbsent",
  create_failed: "create", verification_failed: "generic",
};

export default function NodeContractStatus({ status, canVerify = false, onVerify, onInspectCleanup, busy = false }) {
  const { t } = useLanguage();
  let state = states.has(status?.state) ? status.state : "unavailable";
  if (state === "ready" && status?.ready !== true) state = "unavailable";
  const ready = state === "ready";
  const blocked = state === "cleanup_unknown";
  const working = busy || state === "verifying";
  return (
    <div className="rounded-xl bg-gray-50 dark:bg-slate-800/60 p-3 mb-3 min-w-0 space-y-2">
      <div role="status" aria-atomic="true" className="text-sm leading-relaxed break-words">
        <div className={ready ? "text-emerald-700 dark:text-emerald-300" : "text-amber-800 dark:text-amber-300"}>
          {t(`nodes.contract.${state}`)}
        </div>
        {!ready && !blocked && reasons[status?.reason] && <p className="text-gray-700 dark:text-gray-200">{t(`nodes.contract.reason.${reasons[status.reason]}`)}</p>}
        {!ready && <p className="text-gray-700 dark:text-gray-200">{t(blocked ? "nodes.contract.cleanupHelp" : "nodes.contract.notReadyHelp")}</p>}
        {blocked && status?.probe_identity && <code dir="ltr" className="block text-xs text-gray-600 dark:text-gray-300">{status.probe_identity}</code>}
        {blocked && status?.cleanup_inspection === "present" && <p className="text-amber-700 dark:text-amber-300">{t("nodes.contract.cleanupPresent")}</p>}
        {blocked && status?.cleanup_inspection === "ambiguous" && <p className="text-amber-700 dark:text-amber-300">{t("nodes.contract.cleanupAmbiguous")}</p>}
      </div>
      {canVerify && blocked && status?.cleanup_recovery_available === true && onInspectCleanup && (
        <button type="button" className="btn-secondary min-h-[44px]" disabled={working} onClick={onInspectCleanup}>
          {t(working ? "nodes.contract.inspectingCleanup" : "nodes.contract.inspectCleanup")}
        </button>
      )}
      {canVerify && status?.retry_allowed === true && !blocked && onVerify && (
        <button type="button" className="btn-secondary min-h-[44px]" disabled={working} onClick={onVerify}>
          {t(working ? "nodes.contract.verifying" : "nodes.contract.retry")}
        </button>
      )}
    </div>
  );
}
