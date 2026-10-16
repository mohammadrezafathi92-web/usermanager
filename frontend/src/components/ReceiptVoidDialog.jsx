import React, { useCallback, useEffect, useRef, useState } from "react";
import Modal from "./Modal.jsx";
import {
  previewReceiptVoid, executeReceiptVoid, fetchReceiptVoidStatus, retryReceiptVoid, forceReceiptVoidStep,
} from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";
import { formatToman } from "../utils.js";

// Receipt void (Receipt Void 18, 19): a full preview of everything the void
// will touch, then typed username + reason + the superadmin's password + an
// explicit second confirmation; after execute the steps are polled from the
// status endpoint, and a void in cleanup_required offers retry and force
// separately. The backend decides everything; this only shows and asks.
const TERMINAL = ["completed", "failed", "cleanup_required"];

function codeOf(err) {
  const detail = err?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  return detail?.error || detail?.code || "";
}

export default function ReceiptVoidDialog({ entry, operationId: initialOperation, onClose, onDone }) {
  const { t, language } = useLanguage();
  const money = (value) => formatToman(value || 0, language);
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
  const [force, setForce] = useState(null);           // step_key being forced
  const [pollKey, setPollKey] = useState(0);         // restarts polling after retry / force
  const firstField = useRef(null);
  const onDoneRef = useRef(onDone);
  useEffect(() => { onDoneRef.current = onDone; });

  const describe = useCallback((err) => {
    const status = err?.response?.status;
    const known = codeOf(err);
    if (!err?.response) return t("void.errorNetwork");
    if (status === 403) return t("void.error403");
    if (status === 404) return t("void.error404");
    if (status === 409) return t(`void.code.${known}`) !== `void.code.${known}` ? t(`void.code.${known}`) : t("void.error409");
    if (status === 422) return t(`void.code.${known}`) !== `void.code.${known}` ? t(`void.code.${known}`) : t("void.error422");
    return t("void.error500");
  }, [t]);

  const loadPreview = useCallback(() => {
    setPreview(null); setError("");
    previewReceiptVoid(entry.approval_uuid ? { approval_uuid: entry.approval_uuid } : { ledger_entry_id: entry.id })
      .then(({ data }) => setPreview(data))
      .catch((err) => setError(describe(err)));
  }, [entry, describe]);

  useEffect(() => {
    if (!initialOperation) loadPreview();
  }, [initialOperation, loadPreview]);
  useEffect(() => { if (preview && firstField.current) firstField.current.focus(); }, [preview]);

  // Step progress: poll until the operation stops moving.
  useEffect(() => {
    if (!operationId) return undefined;
    let active = true;
    let timer = null;
    const tick = () => fetchReceiptVoidStatus(operationId)
      .then(({ data }) => {
        if (!active) return;
        setStatus(data);
        if (!TERMINAL.includes(data.state)) timer = setTimeout(tick, 2000);
        else onDoneRef.current?.();
      })
      .catch((err) => { if (active) { setError(describe(err)); timer = setTimeout(tick, 5000); } });
    tick();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [operationId, pollKey, describe]);

  async function execute() {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const { data } = await executeReceiptVoid({
        preview_token: preview.preview_token, typed_username: typed, reason, danger_ack: ack,
      }, password);
      setPassword("");
      setOperationId(data.operation_id);
    } catch (err) {
      const known = codeOf(err);
      if (known === "preview_expired" || known === "preview_state_changed" || known === "preview_token_invalid") {
        setNotice(t("void.previewReloaded")); setAck(false); loadPreview();
      } else {
        setError(describe(err));
      }
      setBusy(false);
    }
  }

  async function retry() {
    setBusy(true); setError("");
    try { await retryReceiptVoid(operationId); setStatus(null); setPollKey((k) => k + 1); }
    catch (err) { setError(describe(err)); }
    finally { setBusy(false); }
  }

  async function forceStep() {
    setBusy(true); setError("");
    try {
      await forceReceiptVoidStep(operationId, { step_key: force.step, reason: force.reason, danger_ack: force.ack },
        force.password);
      setForce(null); setStatus(null); setPollKey((k) => k + 1);
    } catch (err) { setError(describe(err)); }
    finally { setBusy(false); }
  }

  const canExecute = preview && !busy && typed === preview.username && reason.trim() && password && ack
    && !(preview.blockers || []).length;
  const stuck = status?.state === "cleanup_required";
  const forceable = (step) => ["failed", "blocked"].includes(step.state)
    && (step.step_key.startsWith("deprovision:") || step.step_key.startsWith("verify:")
      || step.error_sanitized === "quota_changed_since_reward");

  return <Modal open onClose={() => { if (!busy) onClose(); }} title={t("void.title")} width="max-w-2xl">
    <div className="space-y-4 text-sm">
      {notice && <p role="status" className="text-amber-600">{notice}</p>}
      {error && <p role="alert" className="text-red-600">{error}</p>}

      {operationId ? <section aria-live="polite" className="space-y-3">
        <p className="font-semibold">{t("void.operation", { id: operationId })} - {t(`void.state.${status?.state || "running"}`)}</p>
        {status && <ol className="space-y-1">
          {status.steps.map((step) => <li key={step.step_key} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-gray-100 dark:bg-slate-800 px-3 py-2">
            <span dir="ltr" className="font-mono text-xs">{step.step_key}</span>
            <span className={step.state === "done" ? "text-emerald-600" : ["failed", "blocked"].includes(step.state) ? "text-red-600" : step.state === "abandoned" ? "text-amber-600" : "text-gray-500"}>
              {t(`void.step.${step.state}`)}{step.remote_outcome ? ` (${step.remote_outcome})` : ""}
              {step.error_sanitized ? ` - ${step.error_sanitized}` : ""}
              {step.result?.node_id ? ` - ${t("void.node")} #${step.result.node_id}` : ""}
            </span>
            {stuck && forceable(step) && <button type="button" className="btn-secondary text-xs" disabled={busy}
              onClick={() => setForce({ step: step.step_key, reason: "", password: "", ack: false })}>{t("void.force")}</button>}
          </li>)}
        </ol>}
        {status?.state === "completed" && <p className="text-emerald-600 font-semibold">{t("void.completed")}</p>}
        {stuck && <div className="space-y-2">
          <p className="text-amber-600">{t("void.cleanupRequired")}</p>
          <button type="button" className="btn-secondary" disabled={busy} onClick={retry}>{t("void.retry")}</button>
        </div>}
        {status?.state === "failed" && <button type="button" className="btn-secondary" disabled={busy} onClick={retry}>{t("void.retry")}</button>}
        {force && <fieldset className="space-y-2 rounded-xl border border-red-200 p-3">
          <legend className="px-1 text-red-600">{t("void.forceTitle", { step: force.step })}</legend>
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
        <dl className="grid grid-cols-2 gap-2 rounded-xl bg-gray-100 dark:bg-slate-800 p-4">
          <dt>{t("void.customer")}</dt><dd dir="ltr">{preview.username}{preview.telegram_id ? ` (${preview.telegram_id})` : ""}</dd>
          <dt>{t("void.kind")}</dt><dd>{t(`void.shape.${preview.target_shape}`)}</dd>
          <dt>{t("void.amount")}</dt><dd>{money(preview.amount)}</dd>
          <dt>{t("void.approvalMode")}</dt><dd>{t(`void.mode.${preview.approval_mode}`)}{preview.approved_by_telegram_id ? ` - ${preview.approved_by_telegram_id}` : ""}</dd>
          <dt>{t("void.saleVoided")}</dt><dd>{money(preview.ledger.voidable_sale_amount)}</dd>
          {preview.ledger.reseller_credit_refund && <><dt>{t("void.resellerRefund")}</dt><dd>{money(preview.ledger.reseller_credit_refund.amount)}</dd></>}
          {preview.ledger.discount && <><dt>{t("void.discount")}</dt><dd>{money(preview.ledger.discount.amount)}</dd></>}
          {preview.ledger.card && <><dt>{t("void.card")}</dt><dd>{money(preview.ledger.card.amount)} - #{preview.ledger.card.card_id}</dd></>}
          <dt>{t("void.loyalty")}</dt><dd>{t("void.loyaltyCounts", { events: preview.loyalty.purchase_events.length, rewards: preview.loyalty.rewards_caused.length })}</dd>
          <dt>{t("void.openDebt")}</dt><dd>{money(preview.wallet.open_debt)}</dd>
        </dl>

        <section>
          <h4 className="font-semibold mb-1">{t("void.resources")}</h4>
          {!preview.resources.delete ? <p className="text-gray-500">{t("void.noDeletion")}</p> : <>
            <p>{t(preview.resources.delete.kind === "user" ? "void.deleteAccount" : "void.deletePurchase")}</p>
            <ul className="list-disc ps-5">
              {preview.resources.connections.map((c) => <li key={c.id} dir="ltr">#{c.id} {c.protocol} - {c.node_name || `node ${c.node_id}`}</li>)}
            </ul>
          </>}
        </section>

        {preview.other_approvals_on_account.length > 0 && <p role="note" className="rounded-lg bg-amber-50 dark:bg-amber-500/10 p-3 text-amber-700">
          {t("void.otherApprovals", { count: preview.other_approvals_on_account.length })}
        </p>}

        {preview.wallet.sources.length > 0 && <section>
          <h4 className="font-semibold mb-1">{t("void.wallet")}</h4>
          <div className="table-wrap !mx-0"><table>
            <thead><tr><th>{t("void.source")}</th><th>{t("void.gross")}</th><th>{t("void.clawback")}</th>
              <th>{t("void.debt")}</th><th>{t("void.reopen")}</th><th>{t("void.writeoff")}</th></tr></thead>
            <tbody>{preview.wallet.sources.map((s) => <tr key={s.source_id}>
              <td>{t(`void.origin.${s.origin_kind}`)}</td><td>{money(s.gross)}</td><td>{money(s.projected_clawback)}</td>
              <td>{money(s.projected_debt)}</td><td>{money(s.projected_settlement_reopen)}</td><td>{money(s.projected_writeoff)}</td>
            </tr>)}</tbody>
          </table></div>
          {preview.wallet.sources.some((s) => s.spent_by_debits.length) && <p className="text-xs text-gray-500 mt-1">{t("void.salesStay")}</p>}
        </section>}

        {(preview.blockers || []).map((code) => <p key={code} role="alert" className="text-red-600">{t(`void.code.${code}`)}</p>)}

        <div className="space-y-3">
          <label className="block">{t("void.typeUsername", { username: preview.username })}
            <input ref={firstField} className="input mt-1" dir="ltr" autoComplete="off" value={typed}
              aria-invalid={typed !== "" && typed !== preview.username} onChange={(e) => setTyped(e.target.value)} /></label>
          <label className="block">{t("void.reason")}
            <textarea className="input mt-1" required value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <label className="block">{t("void.password")}
            <input type="password" className="input mt-1" autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} /></label>
          <label className="flex items-start gap-2 text-red-600"><input type="checkbox" checked={ack}
            onChange={(e) => setAck(e.target.checked)} />{t("void.dangerAck")}</label>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn-danger flex-1" disabled={!canExecute} onClick={execute}>
            {t(busy ? "void.running" : "void.execute")}
          </button>
          <button type="button" className="btn-secondary" disabled={busy} onClick={onClose}>{t("common.cancel")}</button>
        </div>
      </>}
    </div>
  </Modal>;
}
