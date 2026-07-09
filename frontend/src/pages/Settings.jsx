import React, { useEffect, useState } from "react";
import { KeyRound, Info, Plus, Trash2, Copy, Power, CreditCard, Bot, RefreshCw, DatabaseBackup, Download, Server, Eye, EyeOff, Upload } from "lucide-react";
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
  fetchBackups,
  runBackup,
  downloadBackup,
  restoreBackup,
  deployRemoteBot,
  stopRemoteBot,
} from "../api/client.js";
import { formatDateTime, formatBytes, copyText, downloadBlob } from "../utils.js";
import { useAuth } from "../context/AuthContext.jsx";

export default function Settings() {
  const { isSuperadmin } = useAuth();
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
    loadKeys();
  }, []);

  const [payment, setPayment] = useState({ payment_card_number: "", payment_card_holder: "", payment_instructions: "", topup_presets: "" });
  const [paymentMsg, setPaymentMsg] = useState(null);
  const [savingPayment, setSavingPayment] = useState(false);

  useEffect(() => {
    fetchPanelSettings().then((res) =>
      setPayment({ payment_card_number: "", payment_card_holder: "", payment_instructions: "", topup_presets: "", ...res.data })
    );
  }, []);

  const submitPayment = async (e) => {
    e.preventDefault();
    setSavingPayment(true);
    setPaymentMsg(null);
    try {
      await updatePanelSettings(payment);
      setPaymentMsg({ type: "ok", text: "اطلاعات پرداخت ذخیره شد" });
    } catch (err) {
      setPaymentMsg({ type: "err", text: err?.response?.data?.detail || "خطا در ذخیره" });
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
      setMessage({ type: "ok", text: "رمز عبور با موفقیت تغییر کرد" });
      setOldPassword("");
      setNewPassword("");
    } catch (err) {
      setMessage({ type: "err", text: err?.response?.data?.detail || "خطا در تغییر رمز عبور" });
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
      setKeyError(err?.response?.data?.detail || "خطا در ساخت کلید");
    } finally {
      setSavingKey(false);
    }
  };

  const onToggleKey = async (id) => {
    await toggleApiKey(id);
    loadKeys();
  };

  const onDeleteKey = async (id) => {
    if (!confirm("این کلید حذف شود؟ ربات‌هایی که از این کلید استفاده می‌کنند دیگر دسترسی نخواهند داشت.")) return;
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
    loadBotSettings();
  }, []);

  const submitBot = async (e) => {
    e.preventDefault();
    setSavingBot(true);
    setBotMsg(null);
    try {
      const res = await updateTelegramBotSettings(botForm);
      const { running, last_error, bot_username } = res.data;
      setBotStatus({ running, last_error, bot_username });
      setBotMsg({ type: "ok", text: "تنظیمات ربات ذخیره شد" });
    } catch (err) {
      setBotMsg({ type: "err", text: err?.response?.data?.detail || "خطا در ذخیره" });
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
      setBotMsg({ type: "ok", text: "دستور ری‌استارت ارسال شد" });
    } catch (err) {
      setBotMsg({ type: "err", text: err?.response?.data?.detail || "خطا" });
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
      setDeployMsg({ type: "ok", text: "ربات با موفقیت روی سرور دوم نصب و اجرا شد" });
      setDeployForm((f) => ({ ...f, ssh_password: "" }));
    } catch (err) {
      setDeployMsg({ type: "err", text: err?.response?.data?.detail || "خطا در نصب ربات روی سرور دوم" });
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
      setDeployMsg({ type: "ok", text: "ربات روی سرور دوم متوقف شد و به همین سرور بازگشت" });
      loadBotSettings();
    } catch (err) {
      setDeployMsg({ type: "err", text: err?.response?.data?.detail || "خطا در توقف ربات روی سرور دوم" });
    } finally {
      setStopping(false);
    }
  };

  const [backups, setBackups] = useState([]);
  const [backupMsg, setBackupMsg] = useState(null);
  const [runningBackup, setRunningBackup] = useState(false);
  const [downloadingFile, setDownloadingFile] = useState(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreMsg, setRestoreMsg] = useState(null);

  const loadBackups = () => fetchBackups().then((res) => setBackups(res.data));
  useEffect(() => {
    loadBackups();
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
          ? `بک‌آپ ساخته و دانلود شد — ارسال به ${sent}/${total} ادمین تلگرام`
          : "بک‌آپ ساخته و دانلود شد (ربات تلگرام تنظیم نشده — چیزی ارسال نشد)",
      });
      loadBackups();
    } catch (err) {
      setBackupMsg({ type: "err", text: err?.response?.data?.detail || "خطا در ساخت بک‌آپ" });
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
      setBackupMsg({ type: "err", text: "خطا در دانلود فایل" });
    } finally {
      setDownloadingFile(null);
    }
  };

  const onRestoreFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (
      !window.confirm(
        "با این کار کل دیتابیس فعلی (همه کاربران، پکیج‌ها، تنظیمات و...) با فایل انتخاب‌شده جایگزین می‌شود و سرویس چند ثانیه ری‌استارت می‌شود. یک بک‌آپ از وضعیت فعلی قبل از جایگزینی خودکار گرفته می‌شود. ادامه می‌دهید؟"
      )
    ) {
      return;
    }
    setRestoring(true);
    setRestoreMsg(null);
    try {
      const res = await restoreBackup(file);
      setRestoreMsg({ type: "ok", text: res.data?.message || "دیتابیس با موفقیت جایگزین شد - سرویس در حال راه‌اندازی مجدد است" });
    } catch (err) {
      setRestoreMsg({ type: "err", text: err?.response?.data?.detail || "خطا در بازگردانی دیتابیس" });
    } finally {
      setRestoring(false);
    }
  };

  const apiBase = `${location.protocol}//${location.host}/api/bot`;

  return (
    <Layout>
      <Topbar title="تنظیمات" subtitle="مدیریت حساب ادمین و دسترسی API" />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
        <div className="card">
          <div className="flex items-center gap-2 mb-4">
            <KeyRound size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">تغییر رمز عبور</h3>
          </div>
          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">رمز عبور فعلی</label>
              <input type="password" className="input" required value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">رمز عبور جدید</label>
              <input type="password" className="input" required minLength={6} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
            </div>
            {message && (
              <div className={`text-sm rounded-lg px-3 py-2 ${message.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
                {message.text}
              </div>
            )}
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? "در حال ذخیره..." : "ذخیره رمز جدید"}
            </button>
          </form>
        </div>

        <div className="card">
          <div className="flex items-center gap-2 mb-4">
            <Info size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">نکات پیکربندی</h3>
          </div>
          <ul className="text-sm text-gray-500 space-y-2 list-disc pr-5">
            <li>فاصله زمانی بررسی مصرف (Polling) از متغیر محیطی POLL_INTERVAL_SECONDS در بک‌اند تنظیم می‌شود.</li>
            <li>حجم مصرفی هر کاربر از تمام اتصالاتش (WireGuard/OpenVPN/L2TP/IKEv2/V2Ray) با هم جمع و از سهمیه مشترک کم می‌شود.</li>
            <li>در صورت اتمام حجم یا انقضا، تمام اتصالات کاربر به‌صورت خودکار روی سرورها غیرفعال می‌شوند.</li>
            <li>برای اتصال به هر سرور Xray، وجود API آماری (Stats API) در تنظیمات آن سرور الزامی است.</li>
          </ul>
        </div>
      </div>

      <div className="card mb-4">
        <div className="flex items-center gap-2 mb-4">
          <CreditCard size={18} className="text-brand-600" />
          <h3 className="font-bold text-gray-700">اطلاعات پرداخت کارت‌به‌کارت (برای ربات فروش)</h3>
        </div>
        <p className="text-xs text-gray-400 mb-4">
          این اطلاعات موقع خرید/تمدید توسط مشتری در ربات تلگرام نمایش داده می‌شود.
        </p>
        <form onSubmit={submitPayment} className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">شماره کارت</label>
            <input
              className="input"
              dir="ltr"
              placeholder="6037-XXXX-XXXX-XXXX"
              value={payment.payment_card_number || ""}
              onChange={(e) => setPayment((p) => ({ ...p, payment_card_number: e.target.value }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">به نام</label>
            <input
              className="input"
              value={payment.payment_card_holder || ""}
              onChange={(e) => setPayment((p) => ({ ...p, payment_card_holder: e.target.value }))}
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">توضیحات اضافه (اختیاری)</label>
            <textarea
              className="input"
              rows={2}
              placeholder="مثلا: بعد از واریز، عکس رسید رو همینجا بفرستید"
              value={payment.payment_instructions || ""}
              onChange={(e) => setPayment((p) => ({ ...p, payment_instructions: e.target.value }))}
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">مبالغ پیشنهادی افزایش اعتبار در ربات (تومان، با کاما جدا)</label>
            <input
              className="input"
              dir="ltr"
              placeholder="50000, 100000, 200000"
              value={payment.topup_presets || ""}
              onChange={(e) => setPayment((p) => ({ ...p, topup_presets: e.target.value }))}
            />
            <p className="text-xs text-gray-400 mt-1">این مقادیر به‌صورت دکمه در فلوی «افزایش اعتبار» ربات نمایش داده می‌شن؛ مشتری همیشه می‌تونه مبلغ دلخواه هم بزنه.</p>
          </div>
          {paymentMsg && (
            <div className={`md:col-span-2 text-sm rounded-lg px-3 py-2 ${paymentMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
              {paymentMsg.text}
            </div>
          )}
          <div className="md:col-span-2">
            <button type="submit" disabled={savingPayment} className="btn-primary">
              {savingPayment ? "در حال ذخیره..." : "ذخیره اطلاعات پرداخت"}
            </button>
          </div>
        </form>
      </div>

      <div className="card mb-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Bot size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">ربات تلگرام</h3>
          </div>
          <span className={`badge ${botStatus.running ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
            {botStatus.running ? `فعال — @${botStatus.bot_username || ""}` : "غیرفعال"}
          </span>
        </div>
        <p className="text-xs text-gray-400 mb-4">
          ربات مستقیما داخل همین پنل اجرا می‌شود؛ نیازی به سرور جدا، فایل .env یا SSH نیست. توکن را از @BotFather بگیرید
          و آیدی عددی تلگرام خودتان (مثلا از @userinfobot) را به‌عنوان ادمین وارد کنید.
        </p>
        {botStatus.last_error && (
          <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2 mb-4">{botStatus.last_error}</div>
        )}
        <form onSubmit={submitBot} className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">توکن ربات (از BotFather)</label>
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
                title={showBotToken ? "پنهان کردن" : "نمایش"}
              >
                {showBotToken ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">آیدی عددی ادمین‌ها (با کاما جدا)</label>
            <input
              className="input"
              dir="ltr"
              placeholder="123456789, 987654321"
              value={botForm.admin_ids || ""}
              onChange={(e) => setBotForm((f) => ({ ...f, admin_ids: e.target.value }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">آیدی چت‌های تایید رسید (اختیاری)</label>
            <input
              className="input"
              dir="ltr"
              placeholder="خالی = همان آیدی ادمین‌ها"
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
            <label htmlFor="bot_enabled" className="text-sm text-gray-600">ربات فعال باشد</label>
          </div>
          <div className="md:col-span-2 flex items-center gap-2">
            <input
              type="checkbox"
              id="customer_bot_enabled"
              checked={botForm.customer_bot_enabled !== false}
              onChange={(e) => setBotForm((f) => ({ ...f, customer_bot_enabled: e.target.checked }))}
            />
            <label htmlFor="customer_bot_enabled" className="text-sm text-gray-600">
              دسترسی مشتری‌ها به ربات فعال باشد — اگر خاموش کنید، فقط ادمین‌ها می‌توانند از ربات استفاده کنند و بقیه پیام «ربات موقتاً در دسترس نیست» می‌بینند
            </label>
          </div>
          {botMsg && (
            <div className={`md:col-span-2 text-sm rounded-lg px-3 py-2 ${botMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
              {botMsg.text}
            </div>
          )}
          <div className="md:col-span-2 flex gap-2">
            <button type="submit" disabled={savingBot} className="btn-primary">
              {savingBot ? "در حال ذخیره..." : "ذخیره و اعمال"}
            </button>
            <button type="button" disabled={restartingBot} className="btn-secondary" onClick={onRestartBot}>
              <RefreshCw size={14} /> {restartingBot ? "..." : "ری‌استارت ربات"}
            </button>
          </div>
        </form>
      </div>

      <div className="card mb-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Server size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">نصب ربات روی سرور دیگر</h3>
          </div>
          <span className={`badge ${remoteStatus.remote_mode ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
            {remoteStatus.remote_mode ? `در حال اجرا روی ${remoteStatus.remote_host}` : "روی همین سرور"}
          </span>
        </div>
        <p className="text-xs text-gray-400 mb-4">
          به‌جای اجرای ربات روی همین سرور، می‌توانید آدرس و رمز روت یک سرور دیگر را بدهید تا ربات به‌صورت خودکار
          (نصب Docker + بالا آوردن کانتینر) روی آن سرور نصب شود؛ ربات همان‌جا اجرا می‌شود ولی همچنان از طریق اینترنت
          به دیتابیس واقعی همین پنل وصل است. چون تلگرام فقط یک نمونه در حال دریافت پیام را برای هر ربات قبول می‌کند،
          با نصب موفق روی سرور دوم، ربات روی همین سرور خودکار متوقف می‌شود.
        </p>
        {deployMsg && (
          <div className={`text-sm rounded-lg px-3 py-2 mb-4 whitespace-pre-wrap ${deployMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
            {deployMsg.text}
          </div>
        )}

        {!remoteStatus.remote_mode ? (
          <form onSubmit={onDeployRemoteBot} className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">آی‌پی سرور دوم</label>
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
              <label className="block text-sm text-gray-600 mb-1">پورت SSH</label>
              <input
                className="input"
                dir="ltr"
                type="number"
                value={deployForm.ssh_port}
                onChange={(e) => setDeployForm((f) => ({ ...f, ssh_port: Number(e.target.value) || 22 }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">یوزرنیم SSH</label>
              <input
                className="input"
                dir="ltr"
                value={deployForm.ssh_username}
                onChange={(e) => setDeployForm((f) => ({ ...f, ssh_username: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">رمز عبور SSH</label>
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
              <label className="block text-sm text-gray-600 mb-1">آدرس این پنل که از سرور دوم قابل دسترسی باشد (اختیاری)</label>
              <input
                className="input"
                dir="ltr"
                placeholder={`خالی = http://IP-همین-سرور:8000`}
                value={deployForm.panel_public_url}
                onChange={(e) => setDeployForm((f) => ({ ...f, panel_public_url: e.target.value }))}
              />
              <p className="text-xs text-gray-400 mt-1">پورت 8000 این سرور باید از بیرون (از سرور دوم) در دسترس باشد.</p>
            </div>
            <div className="md:col-span-2">
              <button type="submit" disabled={deploying} className="btn-primary">
                {deploying ? "در حال نصب... ممکن است چند دقیقه طول بکشد" : "نصب و راه‌اندازی"}
              </button>
            </div>
          </form>
        ) : (
          <form onSubmit={onStopRemoteBot} className="space-y-3">
            <div className="text-sm text-gray-600">
              سرور: <span className="font-mono" dir="ltr">{remoteStatus.remote_host}:{remoteStatus.remote_ssh_port}</span>
              {remoteStatus.remote_deployed_at && (
                <span className="text-gray-400"> — نصب‌شده: {formatDateTime(remoteStatus.remote_deployed_at)}</span>
              )}
            </div>
            <div className="max-w-md">
              <label className="block text-sm text-gray-600 mb-1">رمز عبور SSH سرور دوم (برای توقف لازم است)</label>
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
              {stopping ? "در حال توقف..." : "بازگرداندن ربات به همین سرور"}
            </button>
          </form>
        )}

        {remoteStatus.remote_status && (
          <details className="mt-4">
            <summary className="text-xs text-gray-400 cursor-pointer">گزارش آخرین عملیات</summary>
            <pre className="text-xs text-gray-500 bg-gray-50 rounded-lg p-3 mt-2 whitespace-pre-wrap" dir="ltr">
              {remoteStatus.remote_status}
            </pre>
          </details>
        )}
      </div>

      <div className="card mb-4">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <DatabaseBackup size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">بک‌آپ دیتابیس</h3>
          </div>
          <button className="btn-primary" disabled={runningBackup} onClick={onRunBackup}>
            {runningBackup ? "در حال ساخت..." : "دریافت بک‌آپ فوری"}
          </button>
        </div>
        <p className="text-xs text-gray-400 mb-4">
          هر ۶ ساعت (۴ بار در روز) به‌صورت خودکار از کل دیتابیس بک‌آپ گرفته و برای ادمین‌های ربات تلگرام (تنظیم‌شده در
          بخش بالا) ارسال می‌شود. دکمه بالا یک بک‌آپ فوری می‌سازد، همزمان به تلگرام می‌فرستد و در مرورگر هم دانلود
          می‌کند.
        </p>
        {backupMsg && (
          <div className={`text-sm rounded-lg px-3 py-2 mb-4 ${backupMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
            {backupMsg.text}
          </div>
        )}
        <div className="space-y-2">
          {backups.map((b) => (
            <div key={b.filename} className="flex items-center justify-between border border-gray-100 rounded-xl px-4 py-3">
              <div>
                <div className="font-mono text-sm text-gray-800">{b.filename}</div>
                <div className="text-xs text-gray-400 mt-1">
                  {formatDateTime(b.created_at)} — {formatBytes(b.size_bytes)}
                </div>
              </div>
              <button
                className="btn-secondary"
                disabled={downloadingFile === b.filename}
                onClick={() => onDownloadBackup(b.filename)}
              >
                <Download size={14} /> {downloadingFile === b.filename ? "..." : "دانلود"}
              </button>
            </div>
          ))}
          {backups.length === 0 && <div className="text-center text-gray-400 py-6 text-sm">هنوز بک‌آپی ساخته نشده است</div>}
        </div>

        {isSuperadmin && (
          <div className="mt-6 pt-5 border-t border-gray-100">
            <div className="flex items-center gap-2 mb-2">
              <Upload size={16} className="text-amber-600" />
              <h4 className="font-bold text-gray-700 text-sm">بازگردانی دیتابیس از فایل بکاپ</h4>
            </div>
            <p className="text-xs text-gray-400 mb-3">
              یک فایل بکاپ (<span dir="ltr">.db.gz</span> یا <span dir="ltr">.db</span>) آپلود کنید تا کل دیتابیس با آن
              جایگزین شود. قبل از جایگزینی، از وضعیت فعلی به‌صورت خودکار بک‌آپ گرفته می‌شود. این عملیات مخرب است و فقط
              ادمین اصلی می‌تواند انجامش دهد.
            </p>
            {restoreMsg && (
              <div className={`text-sm rounded-lg px-3 py-2 mb-3 ${restoreMsg.type === "ok" ? "text-emerald-600 bg-emerald-50" : "text-red-500 bg-red-50"}`}>
                {restoreMsg.text}
              </div>
            )}
            <label className={`btn-secondary inline-flex cursor-pointer ${restoring ? "opacity-60 pointer-events-none" : ""}`}>
              <Upload size={14} /> {restoring ? "در حال آپلود و جایگزینی..." : "انتخاب فایل بکاپ و بازگردانی"}
              <input type="file" accept=".gz,.db" className="hidden" onChange={onRestoreFile} disabled={restoring} />
            </label>
          </div>
        )}
      </div>

      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <KeyRound size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">کلیدهای API (برای اتصال ربات)</h3>
          </div>
          <button className="btn-primary" onClick={() => setKeyModalOpen(true)}>
            <Plus size={16} /> کلید جدید
          </button>
        </div>

        <div className="text-xs text-gray-500 bg-gray-50 rounded-xl p-3 mb-4 space-y-1">
          <div>آدرس پایه API ربات: <span className="font-mono">{apiBase}</span></div>
          <div>هدر لازم برای هر درخواست: <span className="font-mono">X-API-Key: &lt;کلید&gt;</span></div>
          <div>مستندات کامل و نمونه درخواست‌ها در فایل README پروژه (بخش «API ربات مشتری») موجود است.</div>
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
                  {copiedId === k.id && <span className="text-emerald-600">کپی شد</span>}
                </div>
                <div className="text-xs text-gray-300 mt-1">
                  ساخته‌شده: {formatDateTime(k.created_at)} — آخرین استفاده: {k.last_used_at ? formatDateTime(k.last_used_at) : "هنوز استفاده نشده"}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className={`badge ${k.enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                  {k.enabled ? "فعال" : "غیرفعال"}
                </span>
                <button className="btn-secondary" onClick={() => onToggleKey(k.id)} title={k.enabled ? "غیرفعال کردن" : "فعال کردن"}>
                  <Power size={14} />
                </button>
                <button className="btn-danger" onClick={() => onDeleteKey(k.id)}>
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
          {keys.length === 0 && <div className="text-center text-gray-400 py-6 text-sm">هنوز کلیدی ساخته نشده است</div>}
        </div>
      </div>

      <Modal open={keyModalOpen} onClose={() => setKeyModalOpen(false)} title="ساخت کلید API جدید">
        <form onSubmit={submitKey} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">برچسب (مثلا: ربات تلگرام فروش)</label>
            <input className="input" required value={newLabel} onChange={(e) => setNewLabel(e.target.value)} />
          </div>
          {keyError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{keyError}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setKeyModalOpen(false)}>
              انصراف
            </button>
            <button type="submit" disabled={savingKey} className="btn-primary">
              {savingKey ? "در حال ساخت..." : "ساخت کلید"}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
