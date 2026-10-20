import React, { useCallback, useEffect, useRef, useState } from "react";
import Modal from "./Modal.jsx";
import {
  previewReceiptVoid, executeReceiptVoid, fetchReceiptVoidStatus, retryReceiptVoid, forceReceiptVoidStep,
} from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";
import { formatToman, formatDateTime } from "../utils.js";
import RenewalSection, { PriorRights, Reserved } from "./ReceiptVoidRenewal.jsx";

// Receipt void (Receipt Void 18, 19): a full preview of everything the void
// will touch, then typed username + reason + the superadmin's password + an
// explicit second confirmation; after execute the steps are polled from the
// status endpoint, and a void in cleanup_required offers retry and force
// separately. The backend decides everything; this only shows and asks.
export const TERMINAL = ["completed", "failed", "cleanup_required"];
const POLL_MS = 2000;

// Receipt void (Receipt Void 19): only a superadmin, only on a sale or a
// top-up row; the row must carry its approval_uuid, otherwise it is shown
// disabled as "not traceable" - never guessed.
export const VOIDABLE_KINDS = ["sale_new", "sale_renew", "wallet_topup"];

export function VoidCell({ entry, t, onOpen }) {
  if (!VOIDABLE_KINDS.includes(entry.kind)) return null;
  if (!entry.approval_uuid) {
    return <button type="button" className="btn-secondary text-xs opacity-60" disabled
      title={t("void.notTraceable")} aria-label={t("void.notTraceable")}>{t("void.notTraceableShort")}</button>;
  }
  const state = entry.approval_state;
  if (["voiding", "cleanup_required", "voided"].includes(state)) {
    return <button type="button" className={`text-xs rounded-full px-2 py-0.5 ${state === "voided" ? "bg-gray-100 text-gray-500 dark:bg-slate-800" : "bg-amber-50 text-amber-700 dark:bg-amber-500/10"}`}
      onClick={() => onOpen(entry, entry.approval_void_operation_id)}>{t(`void.rowState.${state}`)}</button>;
  }
  if (state !== "completed") return null;
  return <button type="button" className="btn-danger text-xs" onClick={() => onOpen(entry, null)}>{t("void.button")}</button>;
}

function codeOf(err) {
  const detail = err?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  return detail?.error || detail?.code || "";
}

function Section({ title, children }) {
  return <section className="space-y-1">
    <h4 className="font-semibold">{title}</h4>
    {children}
  </section>;
}

export default function ReceiptVoidDialog({ entry, operationId: initialOperation, onClose, onDone }) {
  const { t, language } = useLanguage();
  const money = (value) => formatToman(value || 0, language);
  const gb = (bytes) => `${((bytes || 0) / 1024 ** 3).toFixed(1)} GB`;
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [typed, setTyped] = useState("");
  const [reason, setReason] = useState("");
  const [password, setPassword] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [operationId, setOperationId] = useState(initialOperation || null);
  const [status, setStatus] = useState(null);
  const [force, setForce] = useState(null);           // the step being forced
  const [pollKey, setPollKey] = useState(0);         // restarts polling after retry / force
  const busyRef = useRef(false);                      // synchronous double-click guard
  const onDoneRef = useRef(onDone);
  useEffect(() => { onDoneRef.current = onDone; });

  const describe = useCallback((err) => {
    const code = err?.response?.status;
    const known = codeOf(err);
    if (!err?.response) return t("void.errorNetwork");
    const specific = known && t(`void.code.${known}`) !== `void.code.${known}` ? t(`void.code.${known}`) : null;
    if (code === 403) return t("void.error403");
    if (code === 404) return t("void.error404");
    if (code === 409) return specific || t("void.error409");
    if (code === 422) return specific || t("void.error422");
    return t("void.error500");
  }, [t]);

  const loadPreview = useCallback(() => {
    setPreview(null); setError("");
    previewReceiptVoid(entry.approval_uuid ? { approval_uuid: entry.approval_uuid } : { ledger_entry_id: entry.id })
      .then(({ data }) => setPreview(data))
      .catch((err) => setError(describe(err)));
  }, [entry, describe]);

  useEffect(() => {
    if (!operationId) loadPreview();
  }, [operationId, loadPreview]);
  // Initial focus of each mode: the typed-username field once the preview is
  // there, the status heading once an operation runs (Modal focuses on open,
  // before the asynchronous preview exists).
  useEffect(() => {
    const target = document.querySelector('[role="dialog"] [data-autofocus]');
    if (target && !target.contains(document.activeElement)) target.focus();
  }, [preview, operationId]);

  // Step progress: poll until the operation stops moving.
  useEffect(() => {
    if (!operationId) return undefined;
    let active = true;
    let timer = null;
    const tick = () => fetchReceiptVoidStatus(operationId)
      .then(({ data }) => {
        if (!active) return;
        setStatus(data);
        if (!TERMINAL.includes(data.state)) timer = setTimeout(tick, POLL_MS);
        else onDoneRef.current?.();
      })
      .catch((err) => { if (active) { setError(describe(err)); timer = setTimeout(tick, POLL_MS * 2); } });
    tick();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [operationId, pollKey, describe]);

  async function guarded(work) {
    if (busyRef.current) return;                      // the second click of a double click
    busyRef.current = true;
    setBusy(true); setError(""); setNotice("");
    try {
      await work();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  const execute = () => guarded(async () => {
    try {
      const { data } = await executeReceiptVoid({
        preview_token: preview.preview_token, typed_username: typed, reason, danger_ack: ack,
      }, password);
      setPassword("");
      setStatus(null);
      setOperationId(data.operation_id);
      setPollKey((k) => k + 1);
    } catch (err) {
      const known = codeOf(err);
      if (["preview_expired", "preview_state_changed", "preview_token_invalid"].includes(known)) {
        setNotice(t("void.previewReloaded")); setAck(false); loadPreview();
      } else {
        setError(describe(err));
      }
    }
  });

  const retry = () => guarded(async () => {
    try {
      await retryReceiptVoid(operationId);
      setStatus(null); setPollKey((k) => k + 1);
    } catch (err) {
      if (codeOf(err) === "preview_state_changed") {
        // A failed void whose account changed since: re-plan it from a fresh
        // preview and a new confirmation (the backend re-claims the same operation).
        setNotice(t("void.rescopeRequired")); setStatus(null); setOperationId(null); setAck(false);
      } else {
        setError(describe(err));
      }
    }
  });

  const forceStep = () => guarded(async () => {
    try {
      await forceReceiptVoidStep(operationId, { step_key: force.step, reason: force.reason, danger_ack: force.ack },
        force.password);
      setForce(null); setStatus(null); setPollKey((k) => k + 1);
    } catch (err) { setError(describe(err)); }
  });

  const canExecute = preview && !busy && typed === preview.username && reason.trim() && password && ack
    && !(preview.blockers || []).length;
  const stuck = status?.state === "cleanup_required";
  const forceable = (step) => ["failed", "blocked"].includes(step.state)
    && (step.step_key.startsWith("deprovision:") || step.step_key.startsWith("verify:")
      || step.error_sanitized === "quota_changed_since_reward");

  return <Modal open onClose={() => { if (!busy) onClose(); }} dismissible={!busy} title={t("void.title")} width="max-w-2xl">
    <div className="space-y-4 text-sm">
      {notice && <p role="status" className="text-amber-700 dark:text-amber-400">{notice}</p>}
      {error && <p role="alert" className="text-red-700 dark:text-red-400">{error}</p>}

      {operationId ? <section aria-live="polite" className="space-y-3">
        <h4 tabIndex={-1} data-autofocus className="font-semibold outline-none">
          {t("void.operation", { id: operationId })} - {t(`void.state.${status?.state || "running"}`)}
        </h4>
        {status && <ol className="space-y-1">
          {status.steps.map((step) => <li key={step.step_key} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-gray-100 dark:bg-slate-800 px-3 py-2">
            <span dir="ltr" className="font-mono text-xs break-all">{step.step_key}</span>
            <span className={step.state === "done" ? "text-emerald-700 dark:text-emerald-400" : ["failed", "blocked"].includes(step.state) ? "text-red-700 dark:text-red-400" : step.state === "abandoned" ? "text-amber-700 dark:text-amber-400" : "text-gray-600 dark:text-gray-400"}>
              {t(`void.step.${step.state}`)}{step.remote_outcome ? ` (${step.remote_outcome})` : ""}
              {step.error_sanitized ? ` - ${step.error_sanitized}` : ""}
              {step.result?.node_id ? ` - ${t("void.node")} #${step.result.node_id}` : ""}
            </span>
            {stuck && forceable(step) && <button type="button" className="btn-secondary text-xs" disabled={busy}
              onClick={() => setForce({ step: step.step_key, reason: "", password: "", ack: false })}>{t("void.force")}</button>}
          </li>)}
        </ol>}
        {status?.state === "completed" && <p className="text-emerald-700 dark:text-emerald-400 font-semibold">{t("void.completed")}</p>}
        {stuck && <p className="text-amber-700 dark:text-amber-400">{t("void.cleanupRequired")}</p>}
        {(stuck || status?.state === "failed") && <button type="button" className="btn-secondary" disabled={busy}
          onClick={retry}>{t("void.retry")}</button>}
        {force && <fieldset className="space-y-2 rounded-xl border border-red-200 p-3">
          <legend className="px-1 text-red-700 dark:text-red-400">{t("void.forceTitle", { step: force.step })}</legend>
          <p>{t("void.forceWarning")}</p>
          <label className="block">{t("void.reason")}
            <textarea className="input mt-1" value={force.reason} onChange={(e) => setForce({ ...force, reason: e.target.value })} /></label>
          <label className="block">{t("void.password")}
            <input type="password" className="input mt-1" autoComplete="current-password" value={force.password}
              onChange={(e) => setForce({ ...force, password: e.target.value })} /></label>
          <label className="flex items-start gap-2"><input type="checkbox" checked={force.ack}
            onChange={(e) => setForce({ ...force, ack: e.target.checked })} />{t("void.forceAck")}</label>
          <div className="flex gap-2">
            <button type="button" className="btn-danger" disabled={busy || !force.reason.trim() || !force.password || !force.ack} onClick={forceStep}>{t("void.forceConfirm")}</button>
            <button type="button" className="btn-secondary" disabled={busy} onClick={() => setForce(null)}>{t("common.cancel")}</button>
          </div>
        </fieldset>}
        <button type="button" className="btn-secondary w-full" disabled={busy} onClick={onClose}>{t("common.close")}</button>
      </section> : !preview ? (!error && <p role="status">{t("common.loading")}</p>) : <>
        <h4 tabIndex={-1} data-autofocus className="font-semibold text-red-700 dark:text-red-400 outline-none">
          {t("void.previewHeading", { username: preview.username })}
        </h4>
        <p role="note" className="rounded-lg bg-amber-50 dark:bg-amber-500/10 p-3 text-amber-800 dark:text-amber-300">{t("void.purposeNote")}</p>
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-3 gap-y-1 rounded-xl bg-gray-100 dark:bg-slate-800 p-4">
          <dt>{t("void.customer")}</dt><dd><bdi dir="ltr">{preview.username}{preview.telegram_id ? ` (${preview.telegram_id})` : ""}</bdi></dd>
          <dt>{t("void.kind")}</dt><dd>{t(`void.shape.${preview.target_shape}`)}</dd>
          <dt>{t("void.amount")}</dt><dd>{money(preview.amount)}</dd>
          <dt>{t("void.approvalMode")}</dt><dd>{t(`void.mode.${preview.approval_mode}`)}{preview.approved_by_telegram_id ? ` - ${preview.approved_by_telegram_id}` : ""}</dd>
          <dt>{t("void.saleVoided")}</dt><dd>{money(preview.ledger.voidable_sale_amount)}</dd>
          {preview.ledger.reseller_credit_refund && <><dt>{t("void.resellerRefund")}</dt><dd>{money(preview.ledger.reseller_credit_refund.amount)}</dd></>}
          {preview.ledger.discount && <><dt>{t("void.discount")}</dt><dd>{money(preview.ledger.discount.amount)}</dd></>}
          {preview.ledger.card && <><dt>{t("void.card")}</dt><dd>{money(preview.ledger.card.amount)} - #{preview.ledger.card.card_id}</dd></>}
          <dt>{t("void.openDebt")}</dt><dd>{money(preview.wallet.open_debt)}</dd>
        </dl>

        <Section title={t("void.resources")}>
          {!preview.resources.delete ? <>
            <p className="text-gray-600 dark:text-gray-400">
              {t(preview.resources.user_exists ? "void.noDeletion" : "void.accountGone")}</p>
            {preview.resources.renewal && <ul className="list-disc ps-5" aria-label={t("void.purchases")}>
              {preview.resources.purchases.map((p) => <li key={p.id}>
                <bdi dir="ltr">#{p.id}</bdi> {p.package_name || p.comment || ""} - <bdi dir="ltr">{gb(p.used_bytes)} / {p.quota_bytes ? gb(p.quota_bytes) : t("void.unlimited")}</bdi>
                {p.max_concurrent_sessions != null ? ` - ${t("void.cap")}: ${p.max_concurrent_sessions}` : ""}
                <Reserved p={p} t={t} gb={gb} />
              </li>)}
            </ul>}
          </> : <>
            <p>{t(preview.resources.delete.kind === "user" ? "void.deleteAccount" : "void.deletePurchase")}</p>
            <ul className="list-disc ps-5" aria-label={t("void.purchases")}>
              {preview.resources.purchases.map((p) => <li key={p.id}>
                <bdi dir="ltr">#{p.id}</bdi> {p.package_name || p.comment || ""} - <bdi dir="ltr">{gb(p.used_bytes)} / {p.quota_bytes ? gb(p.quota_bytes) : t("void.unlimited")}</bdi>
                {p.expire_at ? <> - <bdi dir="ltr">{formatDateTime(p.expire_at, language)}</bdi></> : ""} ({t(`void.approvalState.${p.status}`) !== `void.approvalState.${p.status}` ? t(`void.approvalState.${p.status}`) : p.status})
                {p.max_concurrent_sessions != null ? ` - ${t("void.cap")}: ${p.max_concurrent_sessions}` : ""}
                <Reserved p={p} t={t} gb={gb} />
              </li>)}
            </ul>
            <ul className="list-disc ps-5" aria-label={t("void.connections")}>
              {preview.resources.connections.map((c) => <li key={c.id}><bdi dir="ltr">#{c.id} {c.protocol} - {c.node_name || `node ${c.node_id}`}</bdi></li>)}
            </ul>
          </>}
        </Section>

        <RenewalSection resources={preview.resources} t={t} gb={gb} language={language} />
        <PriorRights resources={preview.resources} t={t} gb={gb} />

        {preview.other_approvals_on_account.length > 0 && <div role="note" className="rounded-lg bg-amber-50 dark:bg-amber-500/10 p-3 text-amber-800 dark:text-amber-300 space-y-1">
          <p>{t("void.otherApprovals", { count: preview.other_approvals_on_account.length })}</p>
          <ul className="list-disc ps-5">
            {preview.other_approvals_on_account.map((a) => <li key={a.approval_uuid}>
              <bdi dir="ltr" className="font-mono text-xs break-all">{a.approval_uuid}</bdi> - {t(`void.approvalKind.${a.kind}`)} - {t(`void.approvalState.${a.state}`)} - {money(a.amount_snapshot)}
            </li>)}
          </ul>
        </div>}

        <Section title={t("void.ledgerRows")}>
          <ul className="list-disc ps-5">
            {preview.ledger.entries.map((e) => <li key={e.id}>#{e.id} {t(`accounting.kind.${e.kind}`)} - {money(e.amount)}{e.voided ? ` (${t("void.rowState.voided")})` : ""}</li>)}
          </ul>
        </Section>

        <Section title={t("void.effects")}>
          <ul className="list-disc ps-5">
            {preview.effects.map((e) => <li key={e.id} className="text-xs"><bdi dir="ltr">{e.effect_type}:{e.effect_key}{e.delta_value != null ? ` (${e.delta_value})` : ""}</bdi></li>)}
          </ul>
        </Section>

        {preview.wallet.sources.length > 0 && <Section title={t("void.wallet")}>
          {/* Scrolls sideways on a phone: focusable, so the keyboard can scroll it too. */}
          <div className="table-wrap !mx-0" tabIndex={0} role="region" aria-label={t("void.wallet")}><table>
            <thead className="[&_th]:text-gray-700 dark:[&_th]:text-gray-200"><tr><th>{t("void.source")}</th><th>{t("void.gross")}</th><th>{t("void.clawback")}</th>
              <th>{t("void.debt")}</th><th>{t("void.reopen")}</th><th>{t("void.writeoff")}</th></tr></thead>
            <tbody>{preview.wallet.sources.map((s) => <tr key={s.source_id}>
              <td>{t(`void.origin.${s.origin_kind}`)}</td><td>{money(s.gross)}</td><td>{money(s.projected_clawback)}</td>
              <td>{money(s.projected_debt)}</td><td>{money(s.projected_settlement_reopen)}</td><td>{money(s.projected_writeoff)}</td>
            </tr>)}</tbody>
          </table></div>
          {preview.wallet.sources.some((s) => s.allocations.length) && <>
            <p className="text-xs text-gray-600 dark:text-gray-400">{t("void.salesStay")}</p>
            <ul className="list-disc ps-5 text-xs" aria-label={t("void.allocations")}>
              {preview.wallet.sources.flatMap((s) => s.allocations.map((a) => <li key={a.allocation_id}>
                {t("void.allocation", { allocation: a.allocation_id, debit: a.debit_id })} - {money(a.amount)}
              </li>))}
            </ul>
          </>}
        </Section>}

        {preview.loyalty.rewards.length > 0 && <Section title={t("void.loyalty")}>
          <ul className="list-disc ps-5">
            {preview.loyalty.rewards.map((r) => <li key={r.id}>
              {t(r.caused_by_this_approval ? "void.rewardOwn" : "void.rewardOther", { slot: r.slot, required: r.required_active_count })}
              {r.credit_amount ? ` - ${money(r.credit_amount)}` : ""}{r.quota_bytes ? ` - ${gb(r.quota_bytes)}` : ""}
            </li>)}
          </ul>
        </Section>}

        {(preview.blockers || []).map((code) => <p key={code} role="alert" className="text-red-700 dark:text-red-400">{t(`void.code.${code}`)}</p>)}

        <div className="space-y-3">
          <label className="block">{t("void.typeUsername", { username: preview.username })}
            <input className="input mt-1" dir="ltr" autoComplete="off" value={typed}
              aria-invalid={typed !== "" && typed !== preview.username} onChange={(e) => setTyped(e.target.value)} /></label>
          <label className="block">{t("void.reason")}
            <textarea className="input mt-1" required value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <label className="block">{t("void.password")}
            <input type="password" className="input mt-1" autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} /></label>
          <label className="flex items-start gap-2 text-red-700 dark:text-red-400"><input type="checkbox" checked={ack}
            onChange={(e) => setAck(e.target.checked)} />{t(preview.target_shape === "renew_purchase" ? "void.dangerAckRenewal" : "void.dangerAck")}</label>
        </div>
        <div className="flex flex-wrap gap-2 pb-[env(safe-area-inset-bottom)]">
          <button type="button" className="btn-danger flex-1" disabled={!canExecute} onClick={execute}>
            {t(busy ? "void.running" : "void.execute")}
          </button>
          <button type="button" className="btn-secondary" disabled={busy} onClick={onClose}>{t("common.cancel")}</button>
        </div>
      </>}
    </div>
  </Modal>;
}
