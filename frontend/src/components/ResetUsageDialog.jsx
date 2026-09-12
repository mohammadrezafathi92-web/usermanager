import React, { useEffect, useState } from "react";
import Modal from "./Modal.jsx";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

/**
 * «بازنشانی مصرف» - shared by the user list's row action and the per-purchase
 * buttons on the user detail page.
 *
 * Zeroing a used-up quota hands the whole thing back, which is selling it
 * again. It was free, and it was the last free door left after the renewal
 * ones were closed (see backend admin_billing.require_package_to_grant, and
 * the panel owner's "اره اونم پکیجی کن", 2026-09-12) - so a reseller now has
 * to say which package it is sold as, and is charged for it.
 *
 * A superadmin is never charged and does not have to name one: "our meter
 * was wrong, clear it" is a real support action and theirs to make. They get
 * a plain confirmation, which is what this dialog was before.
 *
 * One component rather than two copies because the rule is one rule; the two
 * pages differ only in which endpoint `onConfirm` calls.
 */
export default function ResetUsageDialog({ open, onClose, onConfirm, packages = [], title, note }) {
  const { t } = useLanguage();
  const { isSuperadmin } = useAuth();
  const [packageId, setPackageId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // Cleared on every open, so a package left selected from a previous
  // customer can never be charged against the next one by accident.
  useEffect(() => {
    if (open) {
      setPackageId("");
      setError("");
    }
  }, [open]);

  const submit = async (e) => {
    e.preventDefault();
    if (!isSuperadmin && !packageId) {
      setError(t("userDetail.resetUsagePackageRequired"));
      return;
    }
    setSaving(true);
    setError("");
    try {
      await onConfirm(packageId ? Number(packageId) : null);
      onClose();
    } catch (err) {
      setError(err?.response?.data?.detail || t("userDetail.saveError"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title={title || t("userDetail.resetUsageModalTitle")}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-xs text-gray-400">{note || t("userDetail.resetUsageNote")}</p>

        {!isSuperadmin && (
          <div>
            <label className="block text-sm text-gray-600 mb-1">
              {t("userDetail.fieldPackage")} *
            </label>
            <select className="input" value={packageId} onChange={(e) => setPackageId(e.target.value)}>
              <option value="">{t("userDetail.selectPlaceholder")}</option>
              {packages.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} — {p.quota_gb ? `${p.quota_gb}GB` : t("userDetail.unlimited")}
                </option>
              ))}
            </select>
            <div className="hint">{t("userDetail.resetUsagePackageHint")}</div>
          </div>
        )}

        {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button type="submit" disabled={saving} className="btn-primary">
            {saving ? "..." : t("userDetail.resetUsageConfirmButton")}
          </button>
        </div>
      </form>
    </Modal>
  );
}
