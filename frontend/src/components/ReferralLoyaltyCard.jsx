import React, { useEffect, useState } from "react";
import { Gift } from "lucide-react";
import MoneyInput from "./MoneyInput.jsx";
import { fetchPanelSettings, updatePanelSettings } from "../api/client.js";

// Moved out of Settings.jsx (2026-09-10, user request: "این قسمت بره کنار
// کد تخفیف ها بهتر نیست؟") - referral (رفرال) and loyalty rewards are both
// customer-incentive tools, same family as discount codes, so they now live
// on the DiscountCodes.jsx page instead of buried in Settings' general tab.
//
// Self-contained on purpose, same pattern as Settings.jsx's OwnPaymentCard:
// fetches PanelSettings itself and PUTs back only the fields this card
// owns. routers/panel_settings.py's update_settings does
// payload.model_dump(exclude_unset=True), so sending just these 7 fields
// never touches payment_card_number/support_contact_text/HA/etc. on the
// same row - safe to live in a totally different component/page from the
// rest of PanelSettings.
const EMPTY = {
  referral_referrer_reward_credit: 0, referral_referrer_reward_gb: 0,
  referral_new_user_reward_credit: 0, referral_new_user_reward_gb: 0,
  loyalty_purchase_threshold: 0, loyalty_reward_credit: 0, loyalty_reward_gb: 0,
};

export default function ReferralLoyaltyCard({ t }) {
  const [form, setForm] = useState(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);

  useEffect(() => {
    fetchPanelSettings().then((res) => {
      setForm({ ...EMPTY, ...res.data });
      setLoaded(true);
    });
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setMsg(null);
    try {
      await updatePanelSettings(form);
      setMsg({ type: "ok", text: t("settings.msgPaymentSaved") });
    } catch (err) {
      setMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgSaveError") });
    } finally {
      setSaving(false);
    }
  };

  if (!loaded) return null;

  return (
    <div className="card mb-4">
      <div className="flex items-center gap-2 mb-4">
        <Gift size={18} className="text-brand-600" />
        <h3 className="font-bold text-gray-700">{t("discountCodes.incentivesTitle")}</h3>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("discountCodes.incentivesHint")}</p>
      <form onSubmit={submit} className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="md:col-span-2">
          <p className="text-sm font-medium text-gray-600">{t("settings.referralTitle")}</p>
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.referralReferrerCredit")}</label>
          <MoneyInput
            value={form.referral_referrer_reward_credit ?? 0}
            onChange={(v) => set("referral_referrer_reward_credit", v === "" ? 0 : Number(v))}
          />
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.referralReferrerGb")}</label>
          <input
            type="number" min="0" step="0.1" className="input" dir="ltr"
            value={form.referral_referrer_reward_gb ?? 0}
            onChange={(e) => set("referral_referrer_reward_gb", Number(e.target.value))}
          />
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.referralNewUserCredit")}</label>
          <MoneyInput
            value={form.referral_new_user_reward_credit ?? 0}
            onChange={(v) => set("referral_new_user_reward_credit", v === "" ? 0 : Number(v))}
          />
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.referralNewUserGb")}</label>
          <input
            type="number" min="0" step="0.1" className="input" dir="ltr"
            value={form.referral_new_user_reward_gb ?? 0}
            onChange={(e) => set("referral_new_user_reward_gb", Number(e.target.value))}
          />
        </div>

        <div className="md:col-span-2 border-t border-gray-100 dark:border-slate-800 pt-3 mt-1">
          <p className="text-sm font-medium text-gray-600">{t("settings.loyaltyTitle")}</p>
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.loyaltyThreshold")}</label>
          <input
            type="number" min="0" className="input" dir="ltr"
            placeholder={t("settings.loyaltyThresholdPlaceholder")}
            value={form.loyalty_purchase_threshold ?? 0}
            onChange={(e) => set("loyalty_purchase_threshold", Number(e.target.value))}
          />
        </div>
        <div />
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.loyaltyRewardCredit")}</label>
          <MoneyInput
            value={form.loyalty_reward_credit ?? 0}
            onChange={(v) => set("loyalty_reward_credit", v === "" ? 0 : Number(v))}
          />
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.loyaltyRewardGb")}</label>
          <input
            type="number" min="0" step="0.1" className="input" dir="ltr"
            value={form.loyalty_reward_gb ?? 0}
            onChange={(e) => set("loyalty_reward_gb", Number(e.target.value))}
          />
        </div>

        {msg && (
          <div className={`md:col-span-2 text-sm rounded-lg px-3 py-2 ${msg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
            {msg.text}
          </div>
        )}
        <div className="md:col-span-2">
          <button type="submit" disabled={saving} className="btn-primary">
            {saving ? t("settings.saving") : t("common.save")}
          </button>
        </div>
      </form>
    </div>
  );
}
