import React, { useEffect, useState } from "react";
import { KeyRound, Info, Plus, Trash2, Copy, Power, CreditCard, Bot, RefreshCw, DatabaseBackup, Download, Server, Eye, EyeOff, Upload, Repeat, ChevronDown } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import {
  changePassword,
  fetchApiKeys,
  createApiKey,
  toggleApiKey,
  deleteApiKey,
  fetchPanelSettings,
  updatePanelSettings,
  fetchTelegramBotSettings,
  updateTelegramBotSettings,
  restartTelegramBot,
  fetchMyBot,
  updateMyBot,
  fetchBackups,
  runBackup,
  downloadBackup,
  restoreBackup,
  fetchMyBackups,
  runMyBackup,
  downloadMyBackup,
  deployRemoteBot,
  stopRemoteBot,
  resolveHaFailover,
  changePanelPort,
} from "../api/client.js";
import { formatDateTime, formatBytes, copyText, downloadBlob } from "../utils.js";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

// Same action keys/order as backend/app/telegram_bot/keyboards.py's
// CUSTOMER_MENU_ITEMS - keep in sync if a menu item is ever added/removed.
const CUSTOMER_MENU_ITEM_KEYS = [
  "cust_account",
  "cust_usage",
  "cust_renew",
  "cust_buy",
  "cust_topup",
  "cust_tutorials",
  "cust_referral",
  "cust_support",
  "cust_link",
  "cust_myid",
];

export default function Settings() {
  const { isSuperadmin, can, canAny, role } = useAuth();
  const { t, language } = useLanguage();

  // Each tab requires its own permission (task #230's granular settings
  // split - see permissions.py's PERMISSION_GROUPS.settings) - "general"
  // (password change) stays open to every admin since it's not
  // permission-gated at all. "data" needs EITHER backup or API-key access
  // since it holds both cards, each additionally self-gated below.
  const ALL_SETTINGS_TABS = [
    { id: "general", label: t("settings.tabGeneral"), icon: KeyRound, visible: true },
    { id: "bot", label: t("settings.tabBot"), icon: Bot, visible: can("manage_bot_settings") },
    { id: "server", label: t("settings.tabServer"), icon: Server, visible: can("manage_payment_settings") },
    // Always visible: a superadmin sees the full-DB backup + API keys
    // cards, every non-superadmin sees their own scoped OwnBackupCard
    // unconditionally (no permission checkbox gates it) - manage_api_keys
    // is no longer a grantable checkbox at all (routers/api_keys.py is
    // superadmin-only now, see its docstring).
    { id: "data", label: t("settings.tabData"), icon: DatabaseBackup, visible: true },
  ];
  const SETTINGS_TABS = ALL_SETTINGS_TABS.filter((t) => t.visible);
  const [activeTab, setActiveTab] = useState("general");

  useEffect(() => {
    if (!SETTINGS_TABS.some((tab) => tab.id === activeTab) && SETTINGS_TABS.length > 0) {
      setActiveTab(SETTINGS_TABS[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [SETTINGS_TABS.map((t) => t.id).join(",")]);
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [message, setMessage] = useState(null);
  const [saving, setSaving] = useState(false);

  const [keys, setKeys] = useState([]);
  const [keyModalOpen, setKeyModalOpen] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [keyError, setKeyError] = useState("");
  const [savingKey, setSavingKey] = useState(false);
  const [copiedId, setCopiedId] = useState(null);

  const loadKeys = () => fetchApiKeys().then((res) => setKeys(res.data));
  useEffect(() => {
    // Superadmin-only now (routers/api_keys.py) - ApiKey rows have no
    // owner/scope, so this must never be fetched as a non-superadmin (would
    // 403, and previously leaked every key in the system to anyone who
    // reached this page as a level-2 Admin or a Seller with the checkbox).
    if (isSuperadmin) loadKeys();
  }, [isSuperadmin]);

  const [payment, setPayment] = useState({
    payment_card_number: "", payment_card_holder: "", payment_instructions: "", topup_presets: "",
    support_contact_text: "",
    referral_referrer_reward_credit: 0, referral_referrer_reward_gb: 0,
    referral_new_user_reward_credit: 0, referral_new_user_reward_gb: 0,
    loyalty_purchase_threshold: 0, loyalty_reward_credit: 0, loyalty_reward_gb: 0,
  });
  const [paymentMsg, setPaymentMsg] = useState(null);
  const [savingPayment, setSavingPayment] = useState(false);

  const [ha, setHa] = useState({
    ha_enabled: false,
    ha_mode: "standby",
    ha_peer_url: "",
    ha_peer_api_key: "",
    ha_standby_active: false,
    ha_promoted_at: null,
    ha_last_sync_at: null,
    ha_last_health_ok_at: null,
    ha_last_error: null,
  });
  const [haMsg, setHaMsg] = useState(null);
  const [savingHa, setSavingHa] = useState(false);
  const [resolvingHa, setResolvingHa] = useState(false);

  const [portForm, setPortForm] = useState({
    panel_web_port: 80,
    panel_port_status: null,
    panel_port_changed_at: null,
  });
  const [newPort, setNewPort] = useState("");
  const [changingPort, setChangingPort] = useState(false);
  const [portMsg, setPortMsg] = useState(null);

  // Race condition fix: the initial GET below is async, so if the admin
  // starts typing into e.g. the HA peer-API-key field right after the page
  // opens (very easy to do - open Settings, click straight into the HA
  // card), the response could land WHILE they're typing. The old code did
  // setHa((h) => ({ ...h, ...res.data })) unconditionally, which silently
  // overwrote whatever they'd already typed with the stale server value -
  // then clicking "save" would persist that clobbered value instead of
  // what they actually entered. settingsLoaded gates the editable forms
  // below (disabled until true) so there's no window where the user can
  // type before the merge happens.
  const [settingsLoaded, setSettingsLoaded] = useState(false);

  useEffect(() => {
    fetchPanelSettings().then((res) => {
      setPayment({
        payment_card_number: "", payment_card_holder: "", payment_instructions: "", topup_presets: "",
        support_contact_text: "",
        referral_referrer_reward_credit: 0, referral_referrer_reward_gb: 0,
        referral_new_user_reward_credit: 0, referral_new_user_reward_gb: 0,
        loyalty_purchase_threshold: 0, loyalty_reward_credit: 0, loyalty_reward_gb: 0,
        ...res.data,
      });
      setHa((h) => ({ ...h, ...res.data }));
      setPortForm((p) => ({ ...p, ...res.data }));
      setNewPort(String(res.data.panel_web_port || 80));
      setSettingsLoaded(true);
    });
  }, []);

  const onChangePort = async () => {
    if (
      !window.confirm(
        t("settings.confirmPortChange", { oldPort: portForm.panel_web_port || 80, newPort })
      )
    ) {
      return;
    }
    setChangingPort(true);
    setPortMsg(null);
    try {
      const res = await changePanelPort(Number(newPort));
      setPortForm((p) => ({ ...p, panel_web_port: Number(newPort), panel_port_status: res?.data?.message }));
      setPortMsg({ type: "ok", text: res?.data?.message || t("settings.msgPortChanged") });
    } catch (err) {
      setPortMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgPortChangeError") });
    } finally {
      setChangingPort(false);
    }
  };

  const submitHa = async (e) => {
    e.preventDefault();
    setSavingHa(true);
    setHaMsg(null);
    try {
      const res = await updatePanelSettings({
        ha_enabled: ha.ha_enabled,
        ha_mode: ha.ha_mode,
        ha_peer_url: ha.ha_peer_url,
        ha_peer_api_key: ha.ha_peer_api_key,
      });
      setHa((h) => ({ ...h, ...res.data }));
      setHaMsg({ type: "ok", text: t("settings.msgHaSaved") });
    } catch (err) {
      setHaMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgSaveError") });
    } finally {
      setSavingHa(false);
    }
  };

  const onResolveHa = async () => {
    if (
      !window.confirm(t("settings.confirmResolveHa"))
    ) {
      return;
    }
    setResolvingHa(true);
    setHaMsg(null);
    try {
      await resolveHaFailover();
      setHa((h) => ({ ...h, ha_standby_active: false, ha_promoted_at: null, ha_last_error: null }));
      setHaMsg({ type: "ok", text: t("settings.msgHaResolved") });
    } catch (err) {
      setHaMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgGenericError") });
    } finally {
      setResolvingHa(false);
    }
  };

  const submitPayment = async (e) => {
    e.preventDefault();
    setSavingPayment(true);
    setPaymentMsg(null);
    try {
      await updatePanelSettings(payment);
      setPaymentMsg({ type: "ok", text: t("settings.msgPaymentSaved") });
    } catch (err) {
      setPaymentMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgSaveError") });
    } finally {
      setSavingPayment(false);
    }
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setMessage(null);
    try {
      await changePassword(oldPassword, newPassword);
      setMessage({ type: "ok", text: t("settings.msgPasswordChanged") });
      setOldPassword("");
      setNewPassword("");
    } catch (err) {
      setMessage({ type: "err", text: err?.response?.data?.detail || t("settings.msgPasswordChangeError") });
    } finally {
      setSaving(false);
    }
  };

  const submitKey = async (e) => {
    e.preventDefault();
    setSavingKey(true);
    setKeyError("");
    try {
      await createApiKey(newLabel);
      setNewLabel("");
      setKeyModalOpen(false);
      loadKeys();
    } catch (err) {
      setKeyError(err?.response?.data?.detail || t("settings.msgCreateKeyError"));
    } finally {
      setSavingKey(false);
    }
  };

  const onToggleKey = async (id) => {
    await toggleApiKey(id);
    loadKeys();
  };

  const onDeleteKey = async (id) => {
    if (!confirm(t("settings.confirmDeleteKey"))) return;
    await deleteApiKey(id);
    loadKeys();
  };

  const onCopyKey = async (id, value) => {
    const ok = await copyText(value);
    setCopiedId(ok ? id : null);
    setTimeout(() => setCopiedId(null), 1500);
  };

  const [botForm, setBotForm] = useState({ bot_token: "", admin_ids: "", approval_chat_ids: "", enabled: false });
  const [botStatus, setBotStatus] = useState({ running: false, last_error: null, bot_username: null });
  const [remoteStatus, setRemoteStatus] = useState({ remote_mode: false, remote_host: null, remote_status: null, remote_deployed_at: null });
  const [botMsg, setBotMsg] = useState(null);
  const [savingBot, setSavingBot] = useState(false);
  const [restartingBot, setRestartingBot] = useState(false);
  const [showBotToken, setShowBotToken] = useState(false);

  const loadBotSettings = () =>
    fetchTelegramBotSettings().then((res) => {
      const { running, last_error, bot_username, remote_mode, remote_host, remote_ssh_port, remote_ssh_username, remote_status, remote_deployed_at, ...form } = res.data;
      setBotForm((f) => ({ ...f, ...form }));
      setBotStatus({ running, last_error, bot_username });
      setRemoteStatus({ remote_mode, remote_host, remote_ssh_port, remote_ssh_username, remote_status, remote_deployed_at });
    });

  useEffect(() => {
    // The shared/global bot's GET is now superadmin-only server-side (3-tier
    // hierarchy - a level-2 Admin gets their OWN bot instead, see
    // OwnBotCard) - calling this as a non-superadmin would just 403.
    if (isSuperadmin) loadBotSettings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submitBot = async (e) => {
    e.preventDefault();
    setSavingBot(true);
    setBotMsg(null);
    try {
      const res = await updateTelegramBotSettings(botForm);
      const { running, last_error, bot_username } = res.data;
      setBotStatus({ running, last_error, bot_username });
      setBotMsg({ type: "ok", text: t("settings.msgBotSaved") });
    } catch (err) {
      setBotMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgSaveError") });
    } finally {
      setSavingBot(false);
    }
  };

  const onRestartBot = async () => {
    setRestartingBot(true);
    setBotMsg(null);
    try {
      const res = await restartTelegramBot();
      const { running, last_error, bot_username } = res.data;
      setBotStatus({ running, last_error, bot_username });
      setBotMsg({ type: "ok", text: t("settings.msgRestartSent") });
    } catch (err) {
      setBotMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgGenericError") });
    } finally {
      setRestartingBot(false);
    }
  };

  const [deployForm, setDeployForm] = useState({ host: "", ssh_port: 22, ssh_username: "root", ssh_password: "", panel_public_url: "" });
  const [deploying, setDeploying] = useState(false);
  const [deployMsg, setDeployMsg] = useState(null);
  const [stopPassword, setStopPassword] = useState("");
  const [stopping, setStopping] = useState(false);

  const onDeployRemoteBot = async (e) => {
    e.preventDefault();
    setDeploying(true);
    setDeployMsg(null);
    try {
      const res = await deployRemoteBot(deployForm);
      setRemoteStatus({
        remote_mode: res.data.remote_mode,
        remote_host: res.data.remote_host,
        remote_ssh_port: res.data.remote_ssh_port,
        remote_ssh_username: res.data.remote_ssh_username,
        remote_status: res.data.remote_status,
        remote_deployed_at: res.data.remote_deployed_at,
      });
      setDeployMsg({ type: "ok", text: t("settings.msgRemoteDeploySuccess") });
      setDeployForm((f) => ({ ...f, ssh_password: "" }));
    } catch (err) {
      setDeployMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgRemoteDeployError") });
    } finally {
      setDeploying(false);
    }
  };

  const onStopRemoteBot = async (e) => {
    e.preventDefault();
    setStopping(true);
    setDeployMsg(null);
    try {
      const res = await stopRemoteBot(stopPassword);
      setRemoteStatus({
        remote_mode: res.data.remote_mode,
        remote_host: res.data.remote_host,
        remote_ssh_port: res.data.remote_ssh_port,
        remote_ssh_username: res.data.remote_ssh_username,
        remote_status: res.data.remote_status,
        remote_deployed_at: res.data.remote_deployed_at,
      });
      setStopPassword("");
      setDeployMsg({ type: "ok", text: t("settings.msgRemoteStopSuccess") });
      loadBotSettings();
    } catch (err) {
      setDeployMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgRemoteStopError") });
    } finally {
      setStopping(false);
    }
  };

  const [backups, setBackups] = useState([]);
  const [backupMsg, setBackupMsg] = useState(null);
  const [runningBackup, setRunningBackup] = useState(false);
  // Backup file list starts collapsed - the list can grow to 15 entries and
  // most admins only care about it right after clicking "دریافت بک‌آپ فوری"
  // or when they actually need to download/restore one, not on every visit
  // to Settings.
  const [backupsOpen, setBackupsOpen] = useState(false);
  const [downloadingFile, setDownloadingFile] = useState(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreMsg, setRestoreMsg] = useState(null);

  const loadBackups = () => fetchBackups().then((res) => setBackups(res.data));
  useEffect(() => {
    // Full-DB backup list is superadmin-only server-side now - a
    // non-superadmin gets OwnBackupCard's scoped list instead.
    if (isSuperadmin) loadBackups();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onRunBackup = async () => {
    setRunningBackup(true);
    setBackupMsg(null);
    try {
      const res = await runBackup();
      const filename = (res.headers["content-disposition"] || "").match(/filename="?([^"]+)"?/)?.[1] || "backup.db.gz";
      downloadBlob(filename, res.data);
      const sent = res.headers["x-telegram-sent"];
      const total = res.headers["x-telegram-total"];
      setBackupMsg({
        type: "ok",
        text: total && Number(total) > 0
          ? t("settings.msgBackupSentTelegram", { sent, total })
          : t("settings.msgBackupNoTelegram"),
      });
      loadBackups();
    } catch (err) {
      setBackupMsg({ type: "err", text: err?.response?.data?.detail || t("settings.msgBackupCreateError") });
    } finally {
      setRunningBackup(false);
    }
  };

  const onDownloadBackup = async (filename) => {
    setDownloadingFile(filename);
    try {
      const res = await downloadBackup(filename);
      downloadBlob(filename, res.data);
    } catch (err) {
      setBackupMsg({ type: "err", text: t("settings.msgDownloadError") });
    } finally {
      setDownloadingFile(null);
    }
  };

  const onRestoreFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (
      !window.confirm(t("settings.confirmRestore"))
    ) {
      return;
    }
    setRestoring(true);
    setRestoreMsg(null);
    try {
      const res = await restoreBackup(file);
      setRestoreMsg({ type: "ok", text: res.data?.message || t("settings.msgRestoreSuccess") });
    } catch (err) {
      // nginx/proxy errors (e.g. 413 body-too-large) come back as plain HTML,
      // not JSON, so err.response.data.detail is undefined - fall back to
      // showing the HTTP status at least, instead of a status-less generic
      // message that hides which kind of failure this was.
      const detail = err?.response?.data?.detail;
      const status = err?.response?.status;
      const fallback = status ? `${t("settings.msgRestoreError")} (HTTP ${status})` : t("settings.msgRestoreError");
      setRestoreMsg({ type: "err", text: detail || fallback });
    } finally {
      setRestoring(false);
    }
  };

  const apiBase = `${location.protocol}//${location.host}/api/bot`;

  return (
    <Layout>
      <Topbar title={t("settings.title")} subtitle={t("settings.subtitle")} />

      <div className="flex items-center gap-1 border-b border-gray-100 dark:border-slate-800 mb-4 overflow-x-auto">
        {SETTINGS_TABS.map((tab) => {
          const TabIcon = tab.icon;
          const active = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
                active
                  ? "border-brand-600 text-brand-600"
                  : "border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
              }`}
            >
              <TabIcon size={16} /> {tab.label}
            </button>
          );
        })}
      </div>

      {activeTab === "general" && (
        <>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
        <div className="card">
          <div className="flex items-center gap-2 mb-4">
            <KeyRound size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">{t("settings.changePassword")}</h3>
          </div>
          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("settings.currentPassword")}</label>
              <input type="password" className="input" required value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("settings.newPassword")}</label>
              <input type="password" className="input" required minLength={6} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
            </div>
            {message && (
              <div className={`text-sm rounded-lg px-3 py-2 ${message.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
                {message.text}
              </div>
            )}
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? t("settings.saving") : t("settings.saveNewPassword")}
            </button>
          </form>
        </div>

        <div className="card">
          <div className="flex items-center gap-2 mb-4">
            <Info size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">{t("settings.configTips")}</h3>
          </div>
          <ul className="text-sm text-gray-500 space-y-2 list-disc pr-5">
            <li>{t("settings.tipPoll")}</li>
            <li>{t("settings.tipQuota")}</li>
            <li>{t("settings.tipAutoDisable")}</li>
            <li>{t("settings.tipStatsApi")}</li>
          </ul>
        </div>
      </div>

      <div className="card mb-4">
        <div className="flex items-center gap-2 mb-4">
          <CreditCard size={18} className="text-brand-600" />
          <h3 className="font-bold text-gray-700">{t("settings.paymentInfoTitle")}</h3>
        </div>
        <p className="text-xs text-gray-400 mb-4">
          {t("settings.paymentInfoHint")}
        </p>
        <form onSubmit={submitPayment} className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.cardNumber")}</label>
            <input
              className="input"
              dir="ltr"
              placeholder="6037-XXXX-XXXX-XXXX"
              value={payment.payment_card_number || ""}
              onChange={(e) => setPayment((p) => ({ ...p, payment_card_number: e.target.value }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.cardHolder")}</label>
            <input
              className="input"
              value={payment.payment_card_holder || ""}
              onChange={(e) => setPayment((p) => ({ ...p, payment_card_holder: e.target.value }))}
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">{t("settings.extraInstructions")}</label>
            <textarea
              className="input"
              rows={2}
              placeholder={t("settings.extraInstructionsPlaceholder")}
              value={payment.payment_instructions || ""}
              onChange={(e) => setPayment((p) => ({ ...p, payment_instructions: e.target.value }))}
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">{t("settings.topupPresets")}</label>
            <input
              className="input"
              dir="ltr"
              placeholder="50000, 100000, 200000"
              value={payment.topup_presets || ""}
              onChange={(e) => setPayment((p) => ({ ...p, topup_presets: e.target.value }))}
            />
            <p className="text-xs text-gray-400 mt-1">{t("settings.topupPresetsHint")}</p>
          </div>
          {paymentMsg && (
            <div className={`md:col-span-2 text-sm rounded-lg px-3 py-2 ${paymentMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
              {paymentMsg.text}
            </div>
          )}
          <div className="md:col-span-2">
            <button type="submit" disabled={savingPayment} className="btn-primary">
              {savingPayment ? t("settings.saving") : t("settings.savePaymentInfo")}
            </button>
          </div>
        </form>
      </div>

      <div className="card mb-4">
        <div className="flex items-center gap-2 mb-4">
          <Info size={18} className="text-brand-600" />
          <h3 className="font-bold text-gray-700">{t("settings.growthTitle")}</h3>
        </div>
        <p className="text-xs text-gray-400 mb-4">{t("settings.growthHint")}</p>
        <form onSubmit={submitPayment} className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">{t("settings.supportText")}</label>
            <textarea
              className="input"
              rows={2}
              placeholder={t("settings.supportTextPlaceholder")}
              value={payment.support_contact_text || ""}
              onChange={(e) => setPayment((p) => ({ ...p, support_contact_text: e.target.value }))}
            />
          </div>

          <div className="md:col-span-2 border-t border-gray-100 dark:border-slate-800 pt-3 mt-1">
            <p className="text-sm font-medium text-gray-600 mb-2">{t("settings.referralTitle")}</p>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.referralReferrerCredit")}</label>
            <input
              type="number" min="0" className="input" dir="ltr"
              value={payment.referral_referrer_reward_credit ?? 0}
              onChange={(e) => setPayment((p) => ({ ...p, referral_referrer_reward_credit: Number(e.target.value) }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.referralReferrerGb")}</label>
            <input
              type="number" min="0" step="0.1" className="input" dir="ltr"
              value={payment.referral_referrer_reward_gb ?? 0}
              onChange={(e) => setPayment((p) => ({ ...p, referral_referrer_reward_gb: Number(e.target.value) }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.referralNewUserCredit")}</label>
            <input
              type="number" min="0" className="input" dir="ltr"
              value={payment.referral_new_user_reward_credit ?? 0}
              onChange={(e) => setPayment((p) => ({ ...p, referral_new_user_reward_credit: Number(e.target.value) }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.referralNewUserGb")}</label>
            <input
              type="number" min="0" step="0.1" className="input" dir="ltr"
              value={payment.referral_new_user_reward_gb ?? 0}
              onChange={(e) => setPayment((p) => ({ ...p, referral_new_user_reward_gb: Number(e.target.value) }))}
            />
          </div>

          <div className="md:col-span-2 border-t border-gray-100 dark:border-slate-800 pt-3 mt-1">
            <p className="text-sm font-medium text-gray-600 mb-2">{t("settings.loyaltyTitle")}</p>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.loyaltyThreshold")}</label>
            <input
              type="number" min="0" className="input" dir="ltr"
              placeholder={t("settings.loyaltyThresholdPlaceholder")}
              value={payment.loyalty_purchase_threshold ?? 0}
              onChange={(e) => setPayment((p) => ({ ...p, loyalty_purchase_threshold: Number(e.target.value) }))}
            />
          </div>
          <div />
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.loyaltyRewardCredit")}</label>
            <input
              type="number" min="0" className="input" dir="ltr"
              value={payment.loyalty_reward_credit ?? 0}
              onChange={(e) => setPayment((p) => ({ ...p, loyalty_reward_credit: Number(e.target.value) }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.loyaltyRewardGb")}</label>
            <input
              type="number" min="0" step="0.1" className="input" dir="ltr"
              value={payment.loyalty_reward_gb ?? 0}
              onChange={(e) => setPayment((p) => ({ ...p, loyalty_reward_gb: Number(e.target.value) }))}
            />
          </div>

          <div className="md:col-span-2">
            <button type="submit" disabled={savingPayment} className="btn-primary">
              {savingPayment ? t("settings.saving") : t("settings.savePaymentInfo")}
            </button>
          </div>
        </form>
      </div>
        </>
      )}

      {activeTab === "bot" && (
        <>
      {isSuperadmin && (
        <>
      <div className="card mb-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Bot size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">{t("settings.telegramBotTitle")}</h3>
          </div>
          <span className={`badge ${botStatus.running ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
            {botStatus.running ? t("settings.botActiveStatus", { username: botStatus.bot_username || "" }) : t("settings.botInactiveStatus")}
          </span>
        </div>
        <p className="text-xs text-gray-400 mb-4">
          {t("settings.botDescription")}
        </p>
        {botStatus.last_error && (
          <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2 mb-4">{botStatus.last_error}</div>
        )}
        <form onSubmit={submitBot} className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">{t("settings.botToken")}</label>
            <div className="relative">
              <input
                className="input pl-10"
                dir="ltr"
                type={showBotToken ? "text" : "password"}
                placeholder="123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                value={botForm.bot_token || ""}
                onChange={(e) => setBotForm((f) => ({ ...f, bot_token: e.target.value }))}
              />
              <button
                type="button"
                className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-brand-600"
                onClick={() => setShowBotToken((v) => !v)}
                title={showBotToken ? t("settings.hide") : t("settings.show")}
              >
                {showBotToken ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.adminIds")}</label>
            <input
              className="input"
              dir="ltr"
              placeholder="123456789, 987654321"
              value={botForm.admin_ids || ""}
              onChange={(e) => setBotForm((f) => ({ ...f, admin_ids: e.target.value }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.approvalChatIds")}</label>
            <input
              className="input"
              dir="ltr"
              placeholder={t("settings.approvalChatIdsPlaceholder")}
              value={botForm.approval_chat_ids || ""}
              onChange={(e) => setBotForm((f) => ({ ...f, approval_chat_ids: e.target.value }))}
            />
          </div>
          <div className="md:col-span-2 flex items-center gap-2">
            <input
              type="checkbox"
              id="bot_enabled"
              checked={!!botForm.enabled}
              onChange={(e) => setBotForm((f) => ({ ...f, enabled: e.target.checked }))}
            />
            <label htmlFor="bot_enabled" className="text-sm text-gray-600">{t("settings.botEnabledLabel")}</label>
          </div>
          <div className="md:col-span-2 flex items-center gap-2">
            <input
              type="checkbox"
              id="customer_bot_enabled"
              checked={botForm.customer_bot_enabled !== false}
              onChange={(e) => setBotForm((f) => ({ ...f, customer_bot_enabled: e.target.checked }))}
            />
            <label htmlFor="customer_bot_enabled" className="text-sm text-gray-600">
              {t("settings.customerBotEnabledLabel")}
            </label>
          </div>
          <div className="md:col-span-2">
            <div className="text-sm text-gray-600 mb-2">{t("settings.customerMenuItemsLabel")}</div>
            <div className="grid grid-cols-2 gap-2">
              {CUSTOMER_MENU_ITEM_KEYS.map((key) => {
                const disabledSet = new Set((botForm.customer_menu_disabled_items || "").split(",").map((x) => x.trim()).filter(Boolean));
                const checked = !disabledSet.has(key);
                return (
                  <label key={key} className="flex items-center gap-2 text-sm text-gray-600">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => {
                        const next = new Set(disabledSet);
                        if (e.target.checked) next.delete(key);
                        else next.add(key);
                        setBotForm((f) => ({ ...f, customer_menu_disabled_items: Array.from(next).join(",") }));
                      }}
                    />
                    {t(`settings.customerMenuItem.${key}`)}
                  </label>
                );
              })}
            </div>
          </div>
          {botMsg && (
            <div className={`md:col-span-2 text-sm rounded-lg px-3 py-2 ${botMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
              {botMsg.text}
            </div>
          )}
          <div className="md:col-span-2 flex gap-2">
            <button type="submit" disabled={savingBot} className="btn-primary">
              {savingBot ? t("settings.saving") : t("settings.saveAndApply")}
            </button>
            <button type="button" disabled={restartingBot} className="btn-secondary" onClick={onRestartBot}>
              <RefreshCw size={14} /> {restartingBot ? "..." : t("settings.restartBot")}
            </button>
          </div>
        </form>
      </div>

      <div className="card mb-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Server size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">{t("settings.deployOtherServerTitle")}</h3>
          </div>
          <span className={`badge ${remoteStatus.remote_mode ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
            {remoteStatus.remote_mode ? t("settings.runningOn", { host: remoteStatus.remote_host }) : t("settings.runningHere")}
          </span>
        </div>
        <p className="text-xs text-gray-400 mb-4">
          {t("settings.deployDescription")}
        </p>
        {deployMsg && (
          <div className={`text-sm rounded-lg px-3 py-2 mb-4 whitespace-pre-wrap ${deployMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
            {deployMsg.text}
          </div>
        )}

        {!remoteStatus.remote_mode ? (
          <form onSubmit={onDeployRemoteBot} className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("settings.secondServerIp")}</label>
              <input
                className="input"
                dir="ltr"
                required
                placeholder="1.2.3.4"
                value={deployForm.host}
                onChange={(e) => setDeployForm((f) => ({ ...f, host: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("settings.sshPort")}</label>
              <input
                className="input"
                dir="ltr"
                type="number"
                value={deployForm.ssh_port}
                onChange={(e) => setDeployForm((f) => ({ ...f, ssh_port: Number(e.target.value) || 22 }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("settings.sshUsername")}</label>
              <input
                className="input"
                dir="ltr"
                value={deployForm.ssh_username}
                onChange={(e) => setDeployForm((f) => ({ ...f, ssh_username: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("settings.sshPassword")}</label>
              <input
                className="input"
                dir="ltr"
                type="password"
                required
                value={deployForm.ssh_password}
                onChange={(e) => setDeployForm((f) => ({ ...f, ssh_password: e.target.value }))}
              />
            </div>
            <div className="md:col-span-2">
              <label className="block text-sm text-gray-600 mb-1">{t("settings.panelPublicUrl")}</label>
              <input
                className="input"
                dir="ltr"
                placeholder={t("settings.panelPublicUrlPlaceholder")}
                value={deployForm.panel_public_url}
                onChange={(e) => setDeployForm((f) => ({ ...f, panel_public_url: e.target.value }))}
              />
              <p className="text-xs text-gray-400 mt-1">{t("settings.panelPublicUrlHint")}</p>
            </div>
            <div className="md:col-span-2">
              <button type="submit" disabled={deploying} className="btn-primary">
                {deploying ? t("settings.installing") : t("settings.installAndRun")}
              </button>
            </div>
          </form>
        ) : (
          <form onSubmit={onStopRemoteBot} className="space-y-3">
            <div className="text-sm text-gray-600">
              {t("settings.serverLabel")} <span className="font-mono" dir="ltr">{remoteStatus.remote_host}:{remoteStatus.remote_ssh_port}</span>
              {remoteStatus.remote_deployed_at && (
                <span className="text-gray-400"> — {t("settings.installedAt", { value: formatDateTime(remoteStatus.remote_deployed_at, language) })}</span>
              )}
            </div>
            <div className="max-w-md">
              <label className="block text-sm text-gray-600 mb-1">{t("settings.stopSshPasswordLabel")}</label>
              <input
                className="input"
                dir="ltr"
                type="password"
                required
                value={stopPassword}
                onChange={(e) => setStopPassword(e.target.value)}
              />
            </div>
            <button type="submit" disabled={stopping} className="btn-secondary">
              {stopping ? t("settings.stopping") : t("settings.returnBotHere")}
            </button>
          </form>
        )}

        {remoteStatus.remote_status && (
          <details className="mt-4">
            <summary className="text-xs text-gray-400 cursor-pointer">{t("settings.lastOperationLog")}</summary>
            <pre className="text-xs text-gray-500 bg-gray-50 rounded-lg p-3 mt-2 whitespace-pre-wrap" dir="ltr">
              {remoteStatus.remote_status}
            </pre>
          </details>
        )}
      </div>
        </>
      )}

      {!isSuperadmin && role === "admin" && <OwnBotCard t={t} />}
        </>
      )}

      {activeTab === "server" && (
        <>
      {isSuperadmin && (
        <div className="card mb-4">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <Repeat size={18} className="text-brand-600" />
              <h3 className="font-bold text-gray-700">{t("settings.haTitle")}</h3>
            </div>
            <span
              className={`badge ${
                !ha.ha_enabled
                  ? "bg-gray-100 text-gray-500"
                  : ha.ha_standby_active
                  ? "bg-amber-50 text-amber-600"
                  : "bg-emerald-50 text-emerald-600"
              }`}
            >
              {!ha.ha_enabled
                ? t("settings.haDisabled")
                : ha.ha_standby_active
                ? t("settings.haFailoverActive")
                : ha.ha_mode === "primary"
                ? t("settings.haRolePrimary")
                : t("settings.haRoleStandby")}
            </span>
          </div>
          <p className="text-xs text-gray-400 mb-3">
            {t("settings.haDescription1")}{" "}
            <b>{t("settings.haDescription1Bold")}</b> {t("settings.haDescription1After")}
          </p>
          <p className="text-xs text-gray-400 mb-4">
            {t("settings.haDescription2")}
          </p>

          {ha.ha_standby_active && (
            <div className="text-sm text-amber-700 bg-amber-50 rounded-lg px-3 py-3 mb-4 space-y-2">
              <div>
                {t("settings.haFailoverWarning", {
                  promotedAt: ha.ha_promoted_at ? t("settings.haFailoverPromotedAt", { value: formatDateTime(ha.ha_promoted_at, language) }) : "",
                })}
              </div>
              <button className="btn-secondary" disabled={resolvingHa} onClick={onResolveHa}>
                {resolvingHa ? "..." : t("settings.confirmResetFailover")}
              </button>
            </div>
          )}

          {haMsg && (
            <div className={`text-sm rounded-lg px-3 py-2 mb-4 ${haMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
              {haMsg.text}
            </div>
          )}

          {!settingsLoaded && (
            <div className="text-sm text-gray-400 mb-3">{t("common.loading")}</div>
          )}
          <form onSubmit={submitHa} className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="md:col-span-2 flex items-center gap-2">
              <input
                type="checkbox"
                id="ha_enabled"
                disabled={!settingsLoaded}
                checked={!!ha.ha_enabled}
                onChange={(e) => setHa((h) => ({ ...h, ha_enabled: e.target.checked }))}
              />
              <label htmlFor="ha_enabled" className="text-sm text-gray-600">{t("settings.haEnabledLabel")}</label>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("settings.thisServerRole")}</label>
              <select
                className="input"
                disabled={!settingsLoaded}
                value={ha.ha_mode || "standby"}
                onChange={(e) => setHa((h) => ({ ...h, ha_mode: e.target.value }))}
              >
                <option value="primary">{t("settings.roleOptionPrimary")}</option>
                <option value="standby">{t("settings.roleOptionStandby")}</option>
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("settings.peerServerAddress")}</label>
              <input
                className="input"
                dir="ltr"
                disabled={!settingsLoaded}
                placeholder="http://1.2.3.4:8000"
                value={ha.ha_peer_url || ""}
                onChange={(e) => setHa((h) => ({ ...h, ha_peer_url: e.target.value }))}
              />
            </div>
            <div className="md:col-span-2">
              <label className="block text-sm text-gray-600 mb-1">{t("settings.peerApiKey")}</label>
              <input
                className="input"
                dir="ltr"
                disabled={!settingsLoaded}
                value={ha.ha_peer_api_key || ""}
                onChange={(e) => setHa((h) => ({ ...h, ha_peer_api_key: e.target.value }))}
              />
            </div>
            <div className="md:col-span-2 text-xs text-gray-500 bg-gray-50 rounded-xl p-3 space-y-1">
              <div>{t("settings.lastSuccessfulSync", { value: ha.ha_last_sync_at ? formatDateTime(ha.ha_last_sync_at, language) : "—" })}</div>
              <div>{t("settings.lastHealthCheck", { value: ha.ha_last_health_ok_at ? formatDateTime(ha.ha_last_health_ok_at, language) : "—" })}</div>
              {ha.ha_last_error && <div className="text-red-500">{t("settings.lastErrorLine", { value: ha.ha_last_error })}</div>}
            </div>
            <div className="md:col-span-2">
              <button type="submit" disabled={savingHa || !settingsLoaded} className="btn-primary">
                {savingHa ? t("settings.saving") : t("settings.saveHaSettings")}
              </button>
            </div>
          </form>
        </div>
      )}

      {isSuperadmin && (
        <div className="card mb-4">
          <div className="flex items-center gap-2 mb-4">
            <Server size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">{t("settings.panelPortTitle")}</h3>
          </div>
          <p className="text-xs text-gray-400 mb-4">
            {t("settings.panelPortDescription1")} <b dir="ltr">{portForm.panel_web_port || 80}</b> {t("settings.panelPortDescription2")}
          </p>

          {portMsg && (
            <div className={`text-sm rounded-lg px-3 py-2 mb-4 whitespace-pre-wrap ${portMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
              {portMsg.text}
            </div>
          )}

          <div className="bg-gray-50 rounded-lg p-3 space-y-3">
            <div>
              <label className="block text-xs text-gray-500 mb-1">{t("settings.newPort")}</label>
              <input
                type="number"
                className="input"
                dir="ltr"
                value={newPort}
                onChange={(e) => setNewPort(e.target.value)}
              />
            </div>
            <button type="button" className="btn-primary w-full" disabled={changingPort} onClick={onChangePort}>
              {changingPort ? t("settings.applyingPort") : t("settings.changePanelPort")}
            </button>
            {portForm.panel_port_changed_at && (
              <div className="text-xs text-gray-400">{t("settings.lastPortChange", { value: formatDateTime(portForm.panel_port_changed_at, language) })}</div>
            )}
          </div>
        </div>
      )}
        </>
      )}

      {activeTab === "data" && (
        <>
      {!isSuperadmin && <OwnBackupCard t={t} />}
      {isSuperadmin && can("manage_backup") && (
      <div className="card mb-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <DatabaseBackup size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">{t("settings.dbBackupTitle")}</h3>
          </div>
          <button className="btn-primary" disabled={runningBackup} onClick={onRunBackup}>
            {runningBackup ? t("settings.creatingBackup") : t("settings.getInstantBackup")}
          </button>
        </div>
        <p className="text-xs text-gray-400 mb-4">
          {t("settings.backupDescription")}
        </p>
        {backupMsg && (
          <div className={`text-sm rounded-lg px-3 py-2 mb-4 ${backupMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
            {backupMsg.text}
          </div>
        )}
        <button
          type="button"
          className="w-full flex items-center justify-between border border-gray-100 rounded-xl px-4 py-3 text-sm text-gray-700 hover:bg-gray-50 transition"
          onClick={() => setBackupsOpen((v) => !v)}
        >
          <span className="font-bold">{t("settings.backupListToggle", { count: backups.length })}</span>
          <ChevronDown size={16} className={`transition-transform ${backupsOpen ? "rotate-180" : ""}`} />
        </button>
        {backupsOpen && (
          <div className="space-y-2 mt-2">
            {backups.map((b) => (
              <div key={b.filename} className="flex items-center justify-between border border-gray-100 rounded-xl px-4 py-3">
                <div>
                  <div className="font-mono text-sm text-gray-800">{b.filename}</div>
                  <div className="text-xs text-gray-400 mt-1">
                    {formatDateTime(b.created_at, language)} — {formatBytes(b.size_bytes)}
                  </div>
                </div>
                <button
                  className="btn-secondary"
                  disabled={downloadingFile === b.filename}
                  onClick={() => onDownloadBackup(b.filename)}
                >
                  <Download size={14} /> {downloadingFile === b.filename ? "..." : t("settings.download")}
                </button>
              </div>
            ))}
            {backups.length === 0 && <div className="text-center text-gray-400 py-6 text-sm">{t("settings.noBackupsYet")}</div>}
          </div>
        )}

        {isSuperadmin && (
          <div className="mt-6 pt-5 border-t border-gray-100">
            <div className="flex items-center gap-2 mb-2">
              <Upload size={16} className="text-amber-600" />
              <h4 className="font-bold text-gray-700 text-sm">{t("settings.restoreDbTitle")}</h4>
            </div>
            <p className="text-xs text-gray-400 mb-3">
              {t("settings.restoreDbDescription1")}<span dir="ltr">.db.gz</span> {t("settings.restoreDbDescription2")} <span dir="ltr">.db</span>{t("settings.restoreDbDescription3")}
            </p>
            {restoreMsg && (
              <div className={`text-sm rounded-lg px-3 py-2 mb-3 ${restoreMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
                {restoreMsg.text}
              </div>
            )}
            <label className={`btn-secondary inline-flex cursor-pointer ${restoring ? "opacity-60 pointer-events-none" : ""}`}>
              <Upload size={14} /> {restoring ? t("settings.uploadingRestoring") : t("settings.selectBackupFile")}
              <input type="file" accept=".gz,.db" className="hidden" onChange={onRestoreFile} disabled={restoring} />
            </label>
          </div>
        )}
      </div>
      )}

      {isSuperadmin && (
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <KeyRound size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">{t("settings.apiKeysTitle")}</h3>
          </div>
          <button className="btn-primary" onClick={() => setKeyModalOpen(true)}>
            <Plus size={16} /> {t("settings.newKey")}
          </button>
        </div>

        <div className="text-xs text-gray-500 bg-gray-50 rounded-xl p-3 mb-4 space-y-1">
          <div>{t("settings.apiBaseUrl", { value: "" })}<span className="font-mono">{apiBase}</span></div>
          <div>{t("settings.requiredHeader")} <span className="font-mono">X-API-Key: &lt;key&gt;</span></div>
          <div>{t("settings.docsHint")}</div>
        </div>

        <div className="space-y-2">
          {keys.map((k) => (
            <div key={k.id} className="flex items-center justify-between border border-gray-100 rounded-xl px-4 py-3">
              <div>
                <div className="font-medium text-gray-800 text-sm">{k.label}</div>
                <div className="text-xs text-gray-400 font-mono mt-1 flex items-center gap-2">
                  {k.key}
                  <button onClick={() => onCopyKey(k.id, k.key)} className="text-gray-400 hover:text-brand-600">
                    <Copy size={12} />
                  </button>
                  {copiedId === k.id && <span className="text-emerald-600">{t("userDetail.copied")}</span>}
                </div>
                <div className="text-xs text-gray-300 mt-1">
                  {t("settings.createdLabel", { created: formatDateTime(k.created_at, language), lastUsed: k.last_used_at ? formatDateTime(k.last_used_at, language) : t("settings.neverUsed") })}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className={`badge ${k.enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                  {k.enabled ? t("status.active") : t("status.disabled")}
                </span>
                <button className="btn-secondary" onClick={() => onToggleKey(k.id)} title={k.enabled ? t("settings.disableKey") : t("settings.enableKey")}>
                  <Power size={14} />
                </button>
                <button className="btn-danger" onClick={() => onDeleteKey(k.id)}>
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
          {keys.length === 0 && <div className="text-center text-gray-400 py-6 text-sm">{t("settings.noKeysYet")}</div>}
        </div>
      </div>
      )}
        </>
      )}

      <Modal open={keyModalOpen} onClose={() => setKeyModalOpen(false)} title={t("settings.createApiKeyModalTitle")}>
        <form onSubmit={submitKey} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("settings.labelField")}</label>
            <input className="input" required value={newLabel} onChange={(e) => setNewLabel(e.target.value)} />
          </div>
          {keyError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{keyError}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setKeyModalOpen(false)}>
              {t("common.cancel")}
            </button>
            <button type="submit" disabled={savingKey} className="btn-primary">
              {savingKey ? t("settings.creatingKey") : t("settings.createKey")}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}

// ---------------------------------------------------------------------
// Self-contained (own fetch/state) so they can be dropped into the "bot"/
// "data" tabs above without wiring their state into the giant Settings()
// component - a level-2 Admin's OWN dedicated bot and OWN scoped backup
// (3-tier hierarchy feature, see routers/telegram_bot_settings.py's
// /my-bot and routers/backup.py's my_router).

function OwnBotCard({ t }) {
  const [status, setStatus] = useState(null);
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);

  const load = () =>
    fetchMyBot().then((res) => {
      setStatus(res.data);
      setToken(res.data.bot_token || "");
      setEnabled(res.data.enabled);
    });

  useEffect(() => {
    load();
  }, []);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await updateMyBot({ bot_token: token, enabled });
      setStatus(res.data);
      setMsg({ type: "ok", text: t("settings.myBotSaved") });
    } catch (err) {
      setMsg({ type: "err", text: err?.response?.data?.detail || t("settings.myBotSaveError") });
    } finally {
      setSaving(false);
    }
  };

  if (!status) return null;

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Bot size={18} className="text-brand-600" />
          <h3 className="font-bold text-gray-700">{t("settings.myBotTitle")}</h3>
        </div>
        <span className={`badge ${status.running ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
          {status.running ? t("settings.botActiveStatus", { username: status.bot_username || "" }) : t("settings.botInactiveStatus")}
        </span>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("settings.myBotDescription")}</p>
      {!status.telegram_id_linked && (
        <div className="text-xs text-amber-600 bg-amber-50 rounded-lg px-3 py-2 mb-4">{t("settings.myBotNoTelegramLinked")}</div>
      )}
      {status.last_error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2 mb-4">{status.last_error}</div>}
      <div className="space-y-3">
        <div>
          <label className="block text-sm text-gray-600 mb-1">{t("settings.botToken")}</label>
          <div className="relative">
            <input
              className="input pl-10"
              dir="ltr"
              type={showToken ? "text" : "password"}
              placeholder="123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
            <button
              type="button"
              className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-brand-600"
              onClick={() => setShowToken((v) => !v)}
            >
              {showToken ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-600">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {t("settings.myBotEnabled")}
        </label>
        {msg && (
          <div className={`text-sm rounded-lg px-3 py-2 ${msg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
            {msg.text}
          </div>
        )}
        <button type="button" className="btn-primary" disabled={saving} onClick={save}>
          {saving ? t("common.saving") : t("settings.saveAndRestartMyBot")}
        </button>
      </div>
    </div>
  );
}

function OwnBackupCard({ t }) {
  const [backups, setBackups] = useState([]);
  const [running, setRunning] = useState(false);
  const [msg, setMsg] = useState(null);
  const [open, setOpen] = useState(false);

  const load = () => fetchMyBackups().then((res) => setBackups(res.data));

  useEffect(() => {
    load();
  }, []);

  const onRun = async () => {
    setRunning(true);
    setMsg(null);
    try {
      const res = await runMyBackup();
      downloadBlob(`mybackup_${Date.now()}.json.gz`, res.data);
      setMsg({ type: "ok", text: t("settings.myBackupCreated") });
      load();
    } catch (err) {
      setMsg({ type: "err", text: err?.response?.data?.detail || t("settings.myBackupError") });
    } finally {
      setRunning(false);
    }
  };

  const onDownload = async (filename) => {
    const res = await downloadMyBackup(filename);
    downloadBlob(filename, res.data);
  };

  return (
    <div className="card mb-4">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <DatabaseBackup size={18} className="text-brand-600" />
          <h3 className="font-bold text-gray-700">{t("settings.myBackupTitle")}</h3>
        </div>
        <button className="btn-primary" disabled={running} onClick={onRun}>
          {running ? t("settings.creatingBackup") : t("settings.getInstantBackup")}
        </button>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("settings.myBackupDescription")}</p>
      {msg && (
        <div className={`text-sm rounded-lg px-3 py-2 mb-4 ${msg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
          {msg.text}
        </div>
      )}
      <button
        type="button"
        className="w-full flex items-center justify-between border border-gray-100 rounded-xl px-4 py-3 text-sm text-gray-700 hover:bg-gray-50 transition"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="font-bold">{t("settings.backupListToggle", { count: backups.length })}</span>
        <ChevronDown size={16} className={`transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="space-y-2 mt-2">
          {backups.map((b) => (
            <div key={b.filename} className="flex items-center justify-between border border-gray-100 rounded-xl px-4 py-3">
              <div>
                <div className="text-sm text-gray-700" dir="ltr">{b.filename}</div>
                <div className="text-xs text-gray-400">{formatBytes(b.size_bytes)} · {formatDateTime(b.created_at)}</div>
              </div>
              <button type="button" className="text-gray-400 hover:text-brand-600" onClick={() => onDownload(b.filename)}>
                <Download size={16} />
              </button>
            </div>
          ))}
          {backups.length === 0 && <div className="text-sm text-gray-400 text-center py-4">{t("settings.noBackupsYet")}</div>}
        </div>
      )}
    </div>
  );
}
