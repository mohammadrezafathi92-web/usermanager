import React, { useEffect, useState } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import QRCode from "qrcode";
import { ArrowRight, Plus, Trash2, QrCode, Copy, Download, Check, Wifi, Globe, ShieldCheck, Lock, Save, KeyRound, Power, ShieldEllipsis, RefreshCw } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import QuotaBar from "../components/QuotaBar.jsx";
import {
  fetchUser,
  updateUser,
  bulkUpdateUsers,
  fetchNodes,
  addWireguardConnection,
  addOpenvpnConnection,
  addL2tpConnection,
  addIkev2Connection,
  addSstpConnection,
  addXrayConnection,
  deleteConnection,
  getShareLink,
  updateConnection,
  fetchAdmins,
  fetchRadiusLimitLogs,
} from "../api/client.js";
import { statusLabel, STATUS_STYLES, gbToBytes, bytesToGb, formatBytes, formatDateTime, copyText, downloadTextFile } from "../utils.js";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

// Built from a function (not a plain module-level constant) so the labels
// can react to the active language via t() - see TYPE_META usage below.
function buildTypeMeta(t) {
  return {
    wireguard: { label: `WireGuard (${t("userDetail.mikrotikLabel")})`, icon: Wifi, color: "bg-indigo-50 text-indigo-600" },
    openvpn: { label: `OpenVPN (${t("userDetail.mikrotikLabel")})`, icon: ShieldCheck, color: "bg-teal-50 text-teal-600" },
    l2tp: { label: `L2TP/IPsec (${t("userDetail.mikrotikLabel")})`, icon: Lock, color: "bg-amber-50 text-amber-600" },
    ikev2: { label: `IKEv2/IPsec (${t("userDetail.mikrotikLabel")})`, icon: KeyRound, color: "bg-sky-50 text-sky-600" },
    sstp: { label: `SSTP (${t("userDetail.mikrotikLabel")})`, icon: ShieldEllipsis, color: "bg-rose-50 text-rose-600" },
    xray: { label: "V2Ray / Xray", icon: Globe, color: "bg-purple-50 text-purple-600" },
  };
}

const FILE_EXT = { wireguard: "conf", openvpn: "txt", l2tp: "txt", ikev2: "txt", sstp: "txt" };

export default function UserDetail() {
  const { id } = useParams();
  const { isSuperadmin } = useAuth();
  const { t, language } = useLanguage();
  const TYPE_META = buildTypeMeta(t);
  const navigate = useNavigate();
  const location = useLocation();
  const [user, setUser] = useState(null);
  const [nodes, setNodes] = useState([]);
  const [admins, setAdmins] = useState([]);
  const [editOpen, setEditOpen] = useState(false);
  const [renewOpen, setRenewOpen] = useState(false);
  const [renewForm, setRenewForm] = useState({ add_gb: "", add_days: "", reset_usage: true });
  const [renewSaving, setRenewSaving] = useState(false);
  const [renewError, setRenewError] = useState("");
  const [addConnOpen, setAddConnOpen] = useState(false);
  const [shareData, setShareData] = useState(null);
  const [qrUrl, setQrUrl] = useState(null);
  const [copiedKey, setCopiedKey] = useState(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const [editForm, setEditForm] = useState({
    full_name: "",
    quota_gb: "",
    notes: "",
    expire_mode: "none", // none | date | days_from_now | first_use
    expire_at: "",
    expire_days: "",
    max_concurrent_sessions: "",
    balance: "",
    telegram_id: "",
    owner_admin_id: "",
  });
  const [connNodeId, setConnNodeId] = useState("");
  const [connFlow, setConnFlow] = useState("");
  const [connMaxSessions, setConnMaxSessions] = useState(location.state?.defaultMaxSessions ?? 1);
  const [limitConn, setLimitConn] = useState(null); // connection being edited in the limit modal
  const [limitValue, setLimitValue] = useState(1);
  const [limitLogs, setLimitLogs] = useState([]);

  const load = () => fetchUser(id).then((res) => {
    setUser(res.data);
    let expire_mode = "none";
    if (res.data.expire_days_after_first_use) expire_mode = "first_use";
    else if (res.data.expire_at) expire_mode = "date";
    setEditForm({
      full_name: res.data.full_name || "",
      quota_gb: res.data.total_quota_bytes ? bytesToGb(res.data.total_quota_bytes) : "",
      notes: res.data.notes || "",
      expire_mode,
      expire_at: res.data.expire_at ? res.data.expire_at.slice(0, 10) : "",
      expire_days: res.data.expire_days_after_first_use || "",
      max_concurrent_sessions: "",
      balance: res.data.balance ?? 0,
      telegram_id: res.data.telegram_id ?? "",
      owner_admin_id: res.data.owner_admin_id ?? "",
    });
  });

  useEffect(() => {
    load();
    fetchNodes().then((res) => setNodes(res.data));
    if (isSuperadmin) fetchAdmins().then((res) => setAdmins(res.data));
    fetchRadiusLimitLogs({ user_id: id, limit: 50 })
      .then((res) => setLimitLogs(res.data))
      .catch(() => setLimitLogs([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    const payload = shareData?.link || shareData?.config_text;
    if (!payload) {
      setQrUrl(null);
      return;
    }
    QRCode.toDataURL(payload, { width: 220, margin: 1 })
      .then(setQrUrl)
      .catch(() => setQrUrl(null));
  }, [shareData]);

  if (!user) {
    return (
      <Layout>
        <div className="text-gray-400">{t("common.loading")}</div>
      </Layout>
    );
  }

  const saveEdit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      const payload = {
        full_name: editForm.full_name || null,
        notes: editForm.notes || null,
        total_quota_bytes: editForm.quota_gb ? gbToBytes(editForm.quota_gb) : 0,
      };
      if (editForm.expire_mode === "date") {
        payload.expire_at = editForm.expire_at ? new Date(editForm.expire_at).toISOString() : null;
        payload.clear_expire_days_trigger = true;
      } else if (editForm.expire_mode === "days_from_now") {
        const days = Number(editForm.expire_days) || 0;
        payload.expire_at = days ? new Date(Date.now() + days * 86400000).toISOString() : null;
        payload.clear_expire_days_trigger = true;
      } else if (editForm.expire_mode === "first_use") {
        payload.expire_days_after_first_use = Number(editForm.expire_days) || null;
      } else {
        payload.expire_at = null;
        payload.clear_expire_days_trigger = true;
      }
      if (editForm.max_concurrent_sessions !== "") {
        payload.max_concurrent_sessions = Number(editForm.max_concurrent_sessions);
      }
      if (editForm.balance !== "") {
        payload.balance = Number(editForm.balance);
      }
      payload.telegram_id = editForm.telegram_id !== "" ? Number(editForm.telegram_id) : null;
      if (isSuperadmin) {
        payload.owner_admin_id = editForm.owner_admin_id === "" ? null : Number(editForm.owner_admin_id);
      }
      await updateUser(user.id, payload);
      setEditOpen(false);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || t("userDetail.saveError"));
    } finally {
      setSaving(false);
    }
  };

  // پیش‌فرض‌ها را با سهمیه فعلی کاربر پر می‌کنیم (همون منطق پکیج فعلی) - ادمین
  // می‌تواند قبل از تایید تغییرشان بدهد. تعداد روز پیش‌فرض خالی می‌ماند چون
  // مدت زمان پلن اصلی جایی ذخیره نشده و باید توسط ادمین وارد شود.
  const openRenew = () => {
    setRenewForm({
      add_gb: user.total_quota_bytes ? String(bytesToGb(user.total_quota_bytes)) : "",
      add_days: "",
      reset_usage: true,
    });
    setRenewError("");
    setRenewOpen(true);
  };

  const submitRenew = async (e) => {
    e.preventDefault();
    if (!renewForm.add_gb && !renewForm.add_days) {
      setRenewError(t("userDetail.renewMissingFields"));
      return;
    }
    setRenewSaving(true);
    setRenewError("");
    try {
      await bulkUpdateUsers({
        user_ids: [user.id],
        add_gb: renewForm.add_gb ? Number(renewForm.add_gb) : 0,
        add_days: renewForm.add_days ? Number(renewForm.add_days) : 0,
        reset_usage: renewForm.reset_usage,
        status: "active",
        max_concurrent_sessions: null,
      });
      setRenewOpen(false);
      load();
    } catch (err) {
      setRenewError(err?.response?.data?.detail || t("userDetail.renewError"));
    } finally {
      setRenewSaving(false);
    }
  };

  const selectedNode = nodes.find((n) => n.id === Number(connNodeId));

  // Dedicated open-handler so a stale node/flow/max-sessions selection from
  // a previous "افزودن اتصال" attempt (cancelled or after an error) can't
  // silently persist into the next time this modal is opened.
  const openAddConn = () => {
    setConnNodeId("");
    setConnFlow("");
    setConnMaxSessions(location.state?.defaultMaxSessions ?? 1);
    setError("");
    setAddConnOpen(true);
  };

  const addConnection = async (protocol) => {
    if (!connNodeId) {
      setError(t("userDetail.selectServerRequired"));
      return;
    }
    setSaving(true);
    setError("");
    try {
      if (protocol === "wireguard") {
        await addWireguardConnection(user.id, Number(connNodeId));
      } else if (protocol === "openvpn") {
        await addOpenvpnConnection(user.id, Number(connNodeId), Number(connMaxSessions) || 0);
      } else if (protocol === "l2tp") {
        await addL2tpConnection(user.id, Number(connNodeId), Number(connMaxSessions) || 0);
      } else if (protocol === "ikev2") {
        await addIkev2Connection(user.id, Number(connNodeId), Number(connMaxSessions) || 0);
      } else if (protocol === "sstp") {
        await addSstpConnection(user.id, Number(connNodeId), Number(connMaxSessions) || 0);
      } else {
        await addXrayConnection(user.id, Number(connNodeId), connFlow);
      }
      setAddConnOpen(false);
      setConnNodeId("");
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || t("userDetail.addConnError"));
    } finally {
      setSaving(false);
    }
  };

  const removeConnection = async (connId) => {
    if (!confirm(t("userDetail.deleteConnConfirm"))) return;
    await deleteConnection(user.id, connId);
    load();
  };

  const showShare = async (connId) => {
    const res = await getShareLink(user.id, connId);
    setShareData(res.data);
  };

  const closeShare = () => {
    setShareData(null);
    setQrUrl(null);
  };

  const onCopy = async (key, text) => {
    const ok = await copyText(text);
    setCopiedKey(ok ? key : `${key}-failed`);
    setTimeout(() => setCopiedKey(null), 1500);
  };

  const onDownload = () => {
    if (!shareData?.config_text) return;
    const ext = FILE_EXT[shareData.kind] || "txt";
    downloadTextFile(`${user.username}-${shareData.kind}.${ext}`, shareData.config_text);
  };

  const nodeName = (nodeId) => nodes.find((n) => n.id === nodeId)?.name || `#${nodeId}`;

  const openLimit = (c) => {
    setLimitConn(c);
    setLimitValue(c.max_concurrent_sessions ?? 1);
  };

  const saveLimit = async () => {
    if (!limitConn) return;
    setSaving(true);
    try {
      await updateConnection(user.id, limitConn.id, { max_concurrent_sessions: Number(limitValue) });
      setLimitConn(null);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || t("userDetail.saveError"));
    } finally {
      setSaving(false);
    }
  };

  const unban = async (c) => {
    await updateConnection(user.id, c.id, { banned_until: null });
    load();
  };

  const toggleConnEnabled = async (c) => {
    const next = !c.enabled;
    if (!next && !confirm(t("userDetail.disableConnConfirm"))) return;
    try {
      await updateConnection(user.id, c.id, { enabled: next });
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || t("userDetail.toggleConnError"));
    }
  };

  return (
    <Layout>
      <button onClick={() => navigate("/users")} className="flex items-center gap-1 text-sm text-gray-400 hover:text-gray-600 mb-4">
        <ArrowRight size={14} /> {t("userDetail.backToUsers")}
      </button>

      <Topbar title={user.username} subtitle={user.full_name || t("userDetail.noFullName")} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
        <div className="card lg:col-span-2">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-bold text-gray-700">{t("userDetail.usageHeading")}</h3>
            <span className={`badge ${STATUS_STYLES[user.status]}`}>{statusLabel(user.status, language)}</span>
          </div>
          <QuotaBar used={user.used_bytes} total={user.total_quota_bytes} />
          <div className="text-xs text-gray-400 mt-2">
            {t("userDetail.remaining", { value: user.total_quota_bytes ? formatBytes(Math.max(user.total_quota_bytes - user.used_bytes, 0)) : t("userDetail.unlimited") })}
          </div>
          <div className="text-xs text-gray-400 mt-1">
            {t("userDetail.expiry", {
              value: user.expire_days_after_first_use
                ? t("userDetail.expiryFirstUse", { days: user.expire_days_after_first_use })
                : user.expire_at
                ? formatDateTime(user.expire_at, language)
                : t("userDetail.noExpiry"),
            })}
          </div>
          <div className="text-xs text-gray-400 mt-1">
            {t("userDetail.balance", { value: (user.balance || 0).toLocaleString() })}
          </div>
          {isSuperadmin && (
            <div className="text-xs text-gray-400 mt-1">
              {t("userDetail.ownerAdmin", { value: user.owner_admin_username || t("userDetail.noAdmin") })}
            </div>
          )}
        </div>
        <div className="card flex flex-col justify-center gap-2">
          <button className="btn-secondary" onClick={() => setEditOpen(true)}>
            {t("userDetail.editQuota")}
          </button>
          <button className="btn-primary" onClick={openRenew}>
            <RefreshCw size={14} /> {t("userDetail.quickRenew")}
          </button>
        </div>
      </div>

      <div className="flex items-center justify-between mb-3">
        <h3 className="font-bold text-gray-700">{t("userDetail.connectionsHeading")}</h3>
        <button className="btn-primary" onClick={openAddConn}>
          <Plus size={16} /> {t("userDetail.addConnection")}
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {user.connections.map((c) => {
          const meta = TYPE_META[c.type] || TYPE_META.xray;
          const Icon = meta.icon;
          return (
            <div key={c.id} className="card">
              <div className="flex items-start justify-between mb-3">
                <div className="flex items-center gap-2">
                  <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${meta.color}`}>
                    <Icon size={18} />
                  </div>
                  <div>
                    <div className="font-medium text-gray-800">{meta.label}</div>
                    <div className="text-xs text-gray-400">{nodeName(c.node_id)}</div>
                  </div>
                </div>
                <div className="flex flex-col items-end gap-1">
                  <span className={`badge ${c.enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                    {c.enabled ? t("status.active") : t("status.disabled")}
                  </span>
                  <span className={`badge ${c.online ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-400"}`}>
                    <span className={`inline-block w-1.5 h-1.5 rounded-full ml-1 ${c.online ? "bg-emerald-500" : "bg-gray-300"}`} />
                    {c.online ? t("users.online") : t("users.offline")}
                  </span>
                </div>
              </div>

              <div className="text-xs text-gray-500 space-y-1 mb-3">
                <div>{t("userDetail.thisConnUsage", { value: formatBytes(c.total_bytes) })}</div>
                {c.type === "xray" && <div className="truncate">{t("userDetail.identifier", { value: c.xr_email })}</div>}
                {c.type === "wireguard" && <div>{t("userDetail.internalAddress", { value: c.wg_client_address })}</div>}
                {(c.type === "openvpn" || c.type === "l2tp" || c.type === "ikev2" || c.type === "sstp") && (
                  <>
                    <div>{t("userDetail.username", { value: c.ppp_username })}</div>
                    <div>
                      {t("userDetail.concurrentConn", {
                        used: c.active_session_count ?? 0,
                        max: c.max_concurrent_sessions ? c.max_concurrent_sessions : t("userDetail.unlimited"),
                      })}
                    </div>
                    {c.banned_until && new Date(c.banned_until) > new Date() && (
                      <div className="text-red-500">
                        {t("userDetail.tempBanUntil", { value: formatDateTime(c.banned_until, language) })}{" "}
                        <button className="underline" onClick={() => unban(c)}>
                          {t("userDetail.unban")}
                        </button>
                      </div>
                    )}
                  </>
                )}
                <div>{t("userDetail.createdAt", { value: formatDateTime(c.created_at, language) })}</div>
              </div>

              <div className="flex gap-2">
                <button className="btn-secondary flex-1" onClick={() => showShare(c.id)}>
                  <QrCode size={14} /> {t("userDetail.getConfig")}
                </button>
                {(c.type === "openvpn" || c.type === "l2tp" || c.type === "ikev2" || c.type === "sstp") && (
                  <button className="btn-secondary" title={t("userDetail.concurrentLimitTitle")} onClick={() => openLimit(c)}>
                    <ShieldCheck size={14} />
                  </button>
                )}
                <button
                  className="btn-secondary"
                  title={c.enabled ? t("userDetail.disableConn") : t("userDetail.enableConn")}
                  onClick={() => toggleConnEnabled(c)}
                >
                  <Power size={14} className={c.enabled ? "text-emerald-600" : "text-gray-400"} />
                </button>
                <button className="btn-danger" onClick={() => removeConnection(c.id)}>
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          );
        })}
        {user.connections.length === 0 && (
          <div className="card text-center text-gray-400 col-span-2 py-10">{t("userDetail.noConnections")}</div>
        )}
      </div>

      {limitLogs.length > 0 && (
        <div className="card mt-4">
          <h3 className="font-bold text-gray-700 mb-3">{t("userDetail.limitLogHeading")}</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-400">
                  <th className="text-right font-medium py-2">{t("radiusLogs.colType")}</th>
                  <th className="text-right font-medium py-2">{t("radiusLogs.colConnType")}</th>
                  <th className="text-right font-medium py-2">{t("radiusLogs.colCount")}</th>
                  <th className="text-right font-medium py-2">{t("radiusLogs.colBannedUntil")}</th>
                  <th className="text-right font-medium py-2">{t("radiusLogs.colTime")}</th>
                </tr>
              </thead>
              <tbody>
                {limitLogs.map((l) => (
                  <tr key={l.id} className="border-t border-gray-50">
                    <td className="py-2">
                      <span className={`badge ${l.event_type === "ban" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-600"}`}>
                        {l.event_type === "ban" ? t("radiusLogs.eventBan") : t("radiusLogs.eventReject")}
                      </span>
                    </td>
                    <td className="py-2 text-gray-500">{l.connection_type || "-"}</td>
                    <td className="py-2 text-gray-500">
                      {l.active_count ?? "-"}/{l.limit_value ?? "-"}
                    </td>
                    <td className="py-2 text-gray-500">{l.banned_until ? formatDateTime(l.banned_until, language) : "-"}</td>
                    <td className="py-2 text-gray-500">{formatDateTime(l.created_at, language)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Edit modal */}
      <Modal open={editOpen} onClose={() => setEditOpen(false)} title={t("userDetail.editUserModal")}>
        <form onSubmit={saveEdit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldFullName")}</label>
            <input className="input" value={editForm.full_name} onChange={(e) => setEditForm({ ...editForm, full_name: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldQuota")}</label>
              <input type="number" step="0.1" min="0" className="input" placeholder={t("userDetail.quotaPlaceholder")} value={editForm.quota_gb} onChange={(e) => setEditForm({ ...editForm, quota_gb: e.target.value })} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldMaxConcurrent")}</label>
              <input
                type="number"
                min="0"
                className="input"
                placeholder={t("userDetail.noChangePlaceholder")}
                value={editForm.max_concurrent_sessions}
                onChange={(e) => setEditForm({ ...editForm, max_concurrent_sessions: e.target.value })}
              />
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldBalance")}</label>
            <input
              type="number"
              className="input"
              value={editForm.balance}
              onChange={(e) => setEditForm({ ...editForm, balance: e.target.value })}
            />
          </div>

          {isSuperadmin && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldOwnerAdmin")}</label>
              <select
                className="input"
                value={editForm.owner_admin_id}
                onChange={(e) => setEditForm({ ...editForm, owner_admin_id: e.target.value })}
              >
                <option value="">{t("userDetail.noAdminOnlyMain")}</option>
                {admins.filter((a) => !a.is_superadmin).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.username}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldTelegramId")}</label>
            <input
              type="number"
              className="input"
              placeholder={t("userDetail.telegramIdPlaceholder")}
              dir="ltr"
              value={editForm.telegram_id}
              onChange={(e) => setEditForm({ ...editForm, telegram_id: e.target.value })}
            />
            <div className="text-xs text-gray-400 mt-1">
              {t("userDetail.telegramIdHint")}
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldExpireType")}</label>
            <select
              className="input"
              value={editForm.expire_mode}
              onChange={(e) => setEditForm({ ...editForm, expire_mode: e.target.value })}
            >
              <option value="none">{t("userDetail.expireNone")}</option>
              <option value="date">{t("userDetail.expireDate")}</option>
              <option value="days_from_now">{t("userDetail.expireDaysFromNow")}</option>
              <option value="first_use">{t("userDetail.expireFirstUse")}</option>
            </select>

            {editForm.expire_mode === "date" && (
              <input
                type="date"
                className="input mt-2"
                value={editForm.expire_at}
                onChange={(e) => setEditForm({ ...editForm, expire_at: e.target.value })}
              />
            )}

            {editForm.expire_mode === "days_from_now" && (
              <input
                type="number"
                min="1"
                className="input mt-2"
                placeholder={t("userDetail.daysPlaceholder")}
                value={editForm.expire_days}
                onChange={(e) => setEditForm({ ...editForm, expire_days: e.target.value })}
              />
            )}

            {editForm.expire_mode === "first_use" && (
              <>
                <input
                  type="number"
                  min="1"
                  className="input mt-2"
                  placeholder={t("userDetail.daysPlaceholder")}
                  value={editForm.expire_days}
                  onChange={(e) => setEditForm({ ...editForm, expire_days: e.target.value })}
                />
                <div className="text-xs text-gray-400 mt-1">
                  {t("userDetail.firstUseHint", { days: editForm.expire_days || "N" })}
                </div>
              </>
            )}
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldNotes")}</label>
            <textarea className="input" rows={2} value={editForm.notes} onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })} />
          </div>
          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setEditOpen(false)}>
              {t("common.cancel")}
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              <Save size={16} /> {t("common.save")}
            </button>
          </div>
        </form>
      </Modal>

      {/* Renew (تمدید سریع) modal */}
      <Modal open={renewOpen} onClose={() => setRenewOpen(false)} title={t("userDetail.renewModalTitle")}>
        <form onSubmit={submitRenew} className="space-y-4">
          <p className="text-xs text-gray-400">
            {t("userDetail.renewNote")}
          </p>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldAddGb")}</label>
              <input
                className="input"
                type="number"
                min="0"
                step="any"
                value={renewForm.add_gb}
                onChange={(e) => setRenewForm((f) => ({ ...f, add_gb: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldAddDays")}</label>
              <input
                className="input"
                type="number"
                min="0"
                placeholder={t("userDetail.daysPlaceholder")}
                value={renewForm.add_days}
                onChange={(e) => setRenewForm((f) => ({ ...f, add_days: e.target.value }))}
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="renew_reset_usage"
              checked={renewForm.reset_usage}
              onChange={(e) => setRenewForm((f) => ({ ...f, reset_usage: e.target.checked }))}
            />
            <label htmlFor="renew_reset_usage" className="text-sm text-gray-600">
              {t("userDetail.resetUsageLabel")}
            </label>
          </div>
          {renewError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{renewError}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setRenewOpen(false)}>
              {t("common.cancel")}
            </button>
            <button type="submit" disabled={renewSaving} className="btn-primary">
              <RefreshCw size={16} /> {renewSaving ? "..." : t("userDetail.renew")}
            </button>
          </div>
        </form>
      </Modal>

      {/* Add connection modal */}
      <Modal open={addConnOpen} onClose={() => setAddConnOpen(false)} title={t("userDetail.addConnModalTitle")}>
        <div className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldServer")}</label>
            <select className="input" value={connNodeId} onChange={(e) => setConnNodeId(e.target.value)}>
              <option value="">{t("userDetail.selectPlaceholder")}</option>
              {nodes.map((n) => (
                <option key={n.id} value={n.id}>
                  {n.name} ({n.type === "mikrotik" ? t("userDetail.mikrotikLabel") : "V2Ray/Xray"})
                </option>
              ))}
            </select>
          </div>
          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}

          {selectedNode?.type === "mikrotik" && (
            <>
              <div>
                <label className="block text-sm text-gray-600 mb-1">
                  {t("userDetail.fieldMaxConcurrentMikrotik")}
                </label>
                <input
                  type="number"
                  min="0"
                  className="input"
                  placeholder={t("userDetail.maxConcurrentPlaceholder")}
                  value={connMaxSessions}
                  onChange={(e) => setConnMaxSessions(e.target.value)}
                />
              </div>
              <div className="grid grid-cols-3 gap-2">
                <button disabled={saving} className="btn-secondary" onClick={() => addConnection("wireguard")}>
                  <Wifi size={16} /> WireGuard
                </button>
                <button disabled={saving} className="btn-secondary" onClick={() => addConnection("openvpn")}>
                  <ShieldCheck size={16} /> OpenVPN
                </button>
                <button disabled={saving} className="btn-secondary" onClick={() => addConnection("l2tp")}>
                  <Lock size={16} /> L2TP
                </button>
                <button disabled={saving} className="btn-secondary" onClick={() => addConnection("ikev2")}>
                  <KeyRound size={16} /> IKEv2
                </button>
                <button disabled={saving} className="btn-secondary" onClick={() => addConnection("sstp")}>
                  <ShieldEllipsis size={16} /> SSTP
                </button>
              </div>
            </>
          )}

          {selectedNode?.type === "xray" && (
            <button disabled={saving} className="btn-primary w-full" onClick={() => addConnection("xray")}>
              <Globe size={16} /> {t("userDetail.addVlessButton")}
            </button>
          )}

          {!selectedNode && <div className="text-xs text-gray-400">{t("userDetail.selectServerFirst")}</div>}
        </div>
      </Modal>

      {/* Concurrent-session limit modal */}
      <Modal open={!!limitConn} onClose={() => setLimitConn(null)} title={t("userDetail.maxConcurrentModalTitle")}>
        {limitConn && (
          <div className="space-y-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("userDetail.fieldMaxConcurrentSimple")}</label>
              <input
                type="number"
                min="0"
                className="input"
                placeholder={t("userDetail.maxConcurrentSimplePlaceholder")}
                value={limitValue}
                onChange={(e) => setLimitValue(e.target.value)}
              />
              <div className="text-xs text-gray-400 mt-1">
                {t("userDetail.banHint")}
              </div>
            </div>
            {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
            <div className="flex justify-end gap-2 pt-2">
              <button type="button" className="btn-secondary" onClick={() => setLimitConn(null)}>
                {t("common.cancel")}
              </button>
              <button type="button" disabled={saving} className="btn-primary" onClick={saveLimit}>
                {t("common.save")}
              </button>
            </div>
          </div>
        )}
      </Modal>

      {/* Share modal */}
      <Modal open={!!shareData} onClose={closeShare} title={t("userDetail.shareModalTitle")}>
        {shareData && (
          <div className="space-y-4">
            {qrUrl && (
              <div className="flex flex-col items-center gap-2">
                <img src={qrUrl} alt="QR Code" className="rounded-xl border border-gray-100" width={200} height={200} />
                <div className="text-xs text-gray-400">{t("userDetail.qrHint")}</div>
              </div>
            )}

            {shareData.link && (
              <div>
                <div className="text-xs text-gray-500 mb-1">{t("userDetail.shareLink")}</div>
                <div className="flex gap-2">
                  <input readOnly className="input font-mono text-xs" value={shareData.link} />
                  <button className="btn-secondary" onClick={() => onCopy("link", shareData.link)}>
                    {copiedKey === "link" ? <Check size={14} /> : <Copy size={14} />}
                  </button>
                </div>
                {copiedKey === "link" && <div className="text-xs text-emerald-600 mt-1">{t("userDetail.copied")}</div>}
                {copiedKey === "link-failed" && <div className="text-xs text-red-500 mt-1">{t("userDetail.copyFailed")}</div>}
              </div>
            )}

            {shareData.config_text && (
              <div>
                <div className="text-xs text-gray-500 mb-1">
                  {shareData.kind === "wireguard" ? t("userDetail.wireguardConfigFile") : t("userDetail.connectionInfo")}
                </div>
                <textarea readOnly className="input font-mono text-xs" rows={9} value={shareData.config_text} />
                <div className="flex gap-2 mt-2">
                  <button className="btn-secondary flex-1" onClick={() => onCopy("config", shareData.config_text)}>
                    {copiedKey === "config" ? <Check size={14} /> : <Copy size={14} />} {t("userDetail.copy")}
                  </button>
                  <button className="btn-primary flex-1" onClick={onDownload}>
                    <Download size={14} /> {t("userDetail.downloadFile")}
                  </button>
                </div>
                {copiedKey === "config" && <div className="text-xs text-emerald-600 mt-1">{t("userDetail.copied")}</div>}
                {copiedKey === "config-failed" && <div className="text-xs text-red-500 mt-1">{t("userDetail.copyFailed")}</div>}
              </div>
            )}
          </div>
        )}
      </Modal>
    </Layout>
  );
}
