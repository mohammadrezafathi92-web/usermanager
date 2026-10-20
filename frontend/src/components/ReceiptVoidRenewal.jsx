import React from "react";
import { formatDateTime } from "../utils.js";

// The renewal part of the receipt-void preview (owner decision 2026-10-20):
// a renewal void takes back only that payment's right on that service. This
// shows exactly what the backend planned - before / after, the voided term's
// usage kept apart, the other services kept - and the reason when the share
// cannot be proven. It decides nothing.

function Value({ v, kind, t, gb, language }) {
  if (v === null || v === undefined || v === "") return <span>-</span>;
  if (kind === "bytes") return <bdi dir="ltr">{v ? gb(v) : t("void.unlimited")}</bdi>;
  if (kind === "date") return <bdi dir="ltr">{formatDateTime(v, language)}</bdi>;
  return <bdi dir="ltr">{String(v)}</bdi>;
}

const ROWS = {
  queued: [["reserved_quota_bytes", "void.quota", "bytes"], ["reserved_duration_days", "void.expiry", "days"],
           ["reserved_package_id", "void.package", "plain"], ["reserved_max_concurrent_sessions", "void.cap", "plain"]],
  current_term: [["quota_bytes", "void.quota", "bytes"], ["used_bytes", "void.used", "bytes"],
                 ["expire_at", "void.expiry", "date"], ["package_id", "void.package", "plain"],
                 ["max_concurrent_sessions", "void.cap", "plain"]],
};

export function Reserved({ p, t, gb }) {
  const queued = p.reserved_quota_bytes || p.reserved_duration_days || p.reserved_package_id != null;
  if (!queued) return null;
  return <span className="text-xs text-gray-600 dark:text-gray-400"> - {t("void.reserved", {
    quota: p.reserved_quota_bytes ? gb(p.reserved_quota_bytes) : t("void.unlimited"),
    days: p.reserved_duration_days ?? 0, package: p.reserved_package_id ?? "-",
    cap: p.reserved_max_concurrent_sessions ?? "-",
  })}</span>;
}

export default function RenewalSection({ resources, t, gb, language }) {
  const renewal = resources.renewal;
  if (!renewal) return null;
  const consumption = renewal.consumption || {};
  const rows = ROWS[renewal.case];
  return <section className="space-y-2" aria-label={t("void.renewalTitle")}>
    <h4 className="font-semibold">{t("void.renewalTitle")}</h4>
    {renewal.refused ? <p role="alert" className="text-red-700 dark:text-red-400">
      {t("void.renewalRefused")} {t(`void.code.${renewal.refused}`)}</p>
      : <p>{t(`void.renewalCase.${renewal.case}`)}</p>}
    {rows && renewal.before && <div className="table-wrap !mx-0" tabIndex={0} role="region" aria-label={`${t("void.before")} / ${t("void.after")}`}>
      <table>
        <thead><tr><th>{renewal.case === "queued" ? t("void.queue") : ""}</th><th>{t("void.before")}</th><th>{t("void.after")}</th></tr></thead>
        <tbody>{rows.map(([key, label, kind]) => <tr key={key}>
          <td>{t(label)}</td>
          <td><Value v={renewal.before[key]} kind={kind} t={t} gb={gb} language={language} /></td>
          <td><Value v={renewal.after[key]} kind={kind} t={t} gb={gb} language={language} /></td>
        </tr>)}</tbody>
      </table>
    </div>}
    {(consumption.consumed_bytes != null || consumption.set_aside_bytes != null || consumption.unknown_bytes != null) &&
      <div role="note" className="rounded-lg bg-gray-100 dark:bg-slate-800 p-3 space-y-1">
        <p className="font-semibold">{t("void.consumedTitle")}</p>
        {consumption.consumed_bytes != null && <p>{t("void.consumed", { value: gb(consumption.consumed_bytes) })}</p>}
        {consumption.set_aside_bytes != null && <p>{t("void.setAside", { value: gb(consumption.set_aside_bytes) })}</p>}
        {consumption.unknown_bytes != null && <p>{t("void.unknown", { value: gb(consumption.unknown_bytes) })}</p>}
        {renewal.case === "current_term" && <p className="text-xs text-gray-600 dark:text-gray-400">{t("void.consumedLive")}</p>}
      </div>}
    {(resources.preserved_purchases || []).length > 0 && <>
      <h4 className="font-semibold">{t("void.preservedTitle")}</h4>
      <ul className="list-disc ps-5" aria-label={t("void.preservedTitle")}>
        {resources.preserved_purchases.map((p) => <li key={p.id}>
          <bdi dir="ltr">#{p.id}</bdi> {p.package_name || p.comment || ""} - <bdi dir="ltr">{gb(p.used_bytes)} / {p.quota_bytes ? gb(p.quota_bytes) : t("void.unlimited")}</bdi>
          {p.max_concurrent_sessions != null ? ` - ${t("void.cap")}: ${p.max_concurrent_sessions}` : ""}
          <Reserved p={p} t={t} gb={gb} />
        </li>)}
      </ul>
    </>}
  </section>;
}

export function PriorRights({ resources, t, gb }) {
  const rows = resources.prior_rights?.cutover_snapshots || [];
  if (!rows.length) return null;
  return <section className="space-y-1" aria-label={t("void.priorRightsTitle")}>
    <h4 className="font-semibold">{t("void.priorRightsTitle")}</h4>
    <p className="text-xs text-gray-600 dark:text-gray-400">{t("void.priorRightsNote")}</p>
    <ul className="list-disc ps-5">
      {rows.map((s) => <li key={s.snapshot_id}><bdi dir="ltr">#{s.purchase_id}</bdi> <Reserved p={s} t={t} gb={gb} />
        {" - "}{t(s.consumed ? "void.consumedYes" : "void.consumedNo")}</li>)}
    </ul>
  </section>;
}
