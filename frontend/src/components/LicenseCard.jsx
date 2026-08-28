import React, { useEffect, useState } from "react";
import { ShieldCheck, ShieldAlert, ShieldX, Copy, RefreshCw } from "lucide-react";
import { fetchLicenseStatus, checkLicenseNow, setLicenseKey } from "../api/client.js";
import { formatDateTime, copyText } from "../utils.js";

/**
 * License status + key entry (فاز ۳). Self-contained (own fetch/state), so
 * it can be dropped in two different places unchanged:
 *   - Settings.jsx's "server" tab, for the normal healthy-panel case
 *     (days remaining, fingerprint to send the vendor, pasting the first
 *     key).
 *   - LicenseLockOverlay.jsx, for a superadmin whose ALREADY-OPEN session
 *     just got locked - they can paste a fresh key right there without
 *     needing to log in again (routers/license.py's PUT /key does not
 *     itself check the licence gate, only routers/auth.py's /login does).
 *
 * `onChanged` (optional) is called after a successful key save, so a
 * parent (the overlay) can immediately re-fetch /me instead of waiting for
 * AuthContext's 5-minute background poll.
 */
export default function LicenseCard({ t, language, onChanged }) {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [keyInput, setKeyInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [checking, setChecking] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = () => {
    setLoading(true);
    fetchLicenseStatus()
      .then((res) => {
        setStatus(res.data);
        setLoadError(false);
      })
      .catch(() => setLoadError(true))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const onCopyFingerprint = () => {
    if (!status?.fingerprint) return;
    copyText(status.fingerprint).then((ok) => {
      if (ok) {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      }
    });
  };

  const onCheckNow = () => {
    setChecking(true);
    checkLicenseNow()
      .then((res) => setStatus(res.data))
      .finally(() => setChecking(false));
  };

  const submitKey = async (e) => {
    e.preventDefault();
    setSaving(true);
    setSaveError("");
    try {
      const res = await setLicenseKey(keyInput.trim());
      setStatus(res.data);
      setKeyInput("");
      if (onChanged) onChanged();
    } catch (err) {
      setSaveError(err?.response?.data?.detail || t("license.saveError"));
    } finally {
      setSaving(false);
    }
  };

  if (loading && !status) {
    return (
      <div className="card mb-4">
        <div className="text-sm text-gray-400">{t("common.loading")}</div>
      </div>
    );
  }

  if (loadError && !status) {
    return (
      <div className="card mb-4">
        <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{t("license.loadError")}</div>
      </div>
    );
  }

  if (!status) return null;

  const badge = status.master_install
    ? { tone: "bg-brand-50 text-brand-600", label: t("license.badgeMaster"), Icon: ShieldCheck }
    : status.locked
    ? { tone: "bg-red-50 text-red-600", label: t("license.badgeLocked"), Icon: ShieldX }
    : !status.has_key
    ? { tone: "bg-amber-50 text-amber-600", label: t("license.badgeNoKey"), Icon: ShieldAlert }
    : { tone: "bg-emerald-50 text-emerald-600", label: t("license.badgeActive"), Icon: ShieldCheck };

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <badge.Icon size={18} className="text-brand-600" />
          <h3 className="font-bold text-gray-700">{t("license.title")}</h3>
        </div>
        <span className={`badge ${badge.tone}`}>{badge.label}</span>
      </div>

      {status.message && (
        <p className={`text-sm rounded-lg px-3 py-2 mb-4 ${status.locked ? "text-red-600 bg-red-50" : "text-gray-500 bg-gray-50"}`}>
          {status.message}
        </p>
      )}

      {!status.master_install && (
        <div className="text-xs text-gray-500 bg-gray-50 rounded-xl p-3 mb-4 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div>
              <div className="text-gray-400 mb-0.5">{t("license.fingerprintLabel")}</div>
              <span className="font-mono" dir="ltr">{status.fingerprint}</span>
            </div>
            <button type="button" className="btn-secondary shrink-0" onClick={onCopyFingerprint}>
              <Copy size={14} /> {copied ? t("userDetail.copied") : t("license.copyFingerprint")}
            </button>
          </div>
          {status.license && (
            <div className="pt-2 border-t border-gray-100 space-y-1">
              {status.license.customer && (
                <div>{t("license.customerLabel")} <b>{status.license.customer}</b></div>
              )}
              {status.license.expires_at && (
                <div>{t("license.expiresAtLabel")} {formatDateTime(status.license.expires_at, language)}</div>
              )}
              {typeof status.expires_in_days === "number" && (
                <div>{t("license.daysLeftLabel", { count: status.expires_in_days })}</div>
              )}
              {typeof status.grace_days_left === "number" && status.grace_days_left > 0 && (
                <div className="text-amber-600">{t("license.graceDaysLabel", { count: status.grace_days_left })}</div>
              )}
            </div>
          )}
        </div>
      )}

      {!status.master_install && (
        <form onSubmit={submitKey} className="space-y-2">
          <label className="block text-sm text-gray-600">{t("license.keyField")}</label>
          <textarea
            className="input font-mono text-xs"
            dir="ltr"
            rows={3}
            required
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            placeholder="NETCIP-LIC.eyJ...."
          />
          {saveError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{saveError}</div>}
          <div className="flex items-center gap-2 pt-1">
            <button type="submit" disabled={saving || !keyInput.trim()} className="btn-primary">
              {saving ? t("license.saving") : t("license.saveKey")}
            </button>
            <button type="button" className="btn-secondary" disabled={checking} onClick={onCheckNow}>
              <RefreshCw size={14} className={checking ? "animate-spin" : ""} /> {t("license.checkNow")}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
