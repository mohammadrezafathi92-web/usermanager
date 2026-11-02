import React, { useCallback, useEffect, useState } from "react";
import MoneyInput from "./MoneyInput.jsx";
import { fetchAdminPackagePrices, setAdminPackagePrice } from "../api/client.js";
import { errorText, formatToman } from "../utils.js";

/**
 * One reseller's own cooperation price per package (owner decision
 * 2026-11-02). Every row shows the price this account actually pays AND the
 * rule that decided it, so an own price and the per-GB rate can never be
 * mistaken for one another: own price > per-GB rate > package cooperation
 * price > package price. An empty own price means "not set" (falls back);
 * 0 is a real, free price. A renewal with a package costs the same.
 */
export default function AdminPackagePrices({ adminId, t }) {
  const [rows, setRows] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setError("");
    fetchAdminPackagePrices(adminId)
      .then((res) => {
        setRows(res.data);
        setDrafts(Object.fromEntries(res.data.map((r) => [r.package_id, r.own_price === null ? "" : String(r.own_price)])));
      })
      .catch((err) => { setRows([]); setError(errorText(err, t("packagePrices.loadError"))); });
  }, [adminId, t]);

  useEffect(() => { load(); }, [load]);

  const save = async (packageId, value) => {
    setBusy(packageId);
    setError("");
    try {
      const res = await setAdminPackagePrice(adminId, packageId, value === "" ? null : Number(value));
      setRows((list) => list.map((r) => (r.package_id === packageId ? res.data : r)));
      setDrafts((d) => ({ ...d, [packageId]: res.data.own_price === null ? "" : String(res.data.own_price) }));
    } catch (err) {
      setError(errorText(err, t("packagePrices.saveError")));
    } finally {
      setBusy(null);
    }
  };

  const hasRate = rows?.some((r) => r.source === "per_gb_rate" || r.source === "unpriceable");

  return (
    <div>
      <div className="section-title mb-1">{t("packagePrices.title")}</div>
      <div className="hint mb-2">{t("packagePrices.hint")}</div>
      {hasRate && <div className="hint mb-2 text-amber-600 dark:text-amber-400">{t("packagePrices.rateNote")}</div>}
      {error && <div className="text-sm text-red-500 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2 mb-2">{error}</div>}
      {rows === null ? (
        <div className="text-xs text-gray-400 py-3">{t("common.loading")}</div>
      ) : rows.length === 0 ? (
        <div className="empty-state">{t("packagePrices.none")}</div>
      ) : (
        <div className="max-h-72 overflow-y-auto divide-y divide-gray-100 dark:divide-slate-800 border border-gray-100 rounded-xl">
          {rows.map((r) => {
            const draft = drafts[r.package_id] ?? "";
            const saved = r.own_price === null ? "" : String(r.own_price);
            return (
              <div key={r.package_id} className="px-3 py-2 text-xs space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-gray-700 dark:text-gray-200 min-w-0 break-words">{r.package_name}</span>
                  <span className="text-gray-500 shrink-0" dir="ltr">
                    {r.price === null ? "—" : formatToman(r.price)}
                  </span>
                </div>
                <div className="text-gray-400">{t(`packagePrices.source.${r.source}`)}</div>
                <div className="flex items-center gap-2">
                  <MoneyInput
                    small
                    className="flex-1"
                    aria-label={t("packagePrices.ownPriceFor", { name: r.package_name })}
                    placeholder={t("packagePrices.notSet")}
                    value={draft}
                    onChange={(v) => setDrafts((d) => ({ ...d, [r.package_id]: v }))}
                  />
                  <button type="button" className="btn-secondary shrink-0" disabled={busy !== null || draft === saved}
                    onClick={() => save(r.package_id, draft)}>
                    {busy === r.package_id ? "..." : t("common.save")}
                  </button>
                  {r.own_price !== null && (
                    <button type="button" className="btn-secondary shrink-0" disabled={busy !== null}
                      onClick={() => save(r.package_id, "")}>
                      {t("packagePrices.clear")}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
