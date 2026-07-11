import React, { useEffect, useState } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import QRCode from "qrcode";
import { ArrowRight, Plus, Trash2, QrCode, Copy, Download, Check, Wifi, Globe, ShieldCheck, Lock, Save, KeyRound, Power } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import QuotaBar from "../components/QuotaBar.jsx";
import {
  fetchUser,
  updateUser,
  fetchNodes,
  addWireguardConnection,
  addOpenvpnConnection,
  addL2tpConnection,
  addIkev2Connection,
  addXrayConnection,
  deleteConnection,
  getShareLink,
  updateConnection,
  fetchAdmins,
} from "../api/client.js";
import { STATUS_LABELS, STATUS_STYLES, gbToBytes, bytesToGb, formatBytes, formatDateTime, copyText, downloadTextFile } from "../utils.js";
import { useAuth } from "../context/AuthContext.jsx";

const TYPE_META = {
  wireguard: { label: "WireGuard (میکروتیک)", icon: Wifi, color: "bg-indigo-50 text-indigo-600" },
  openvpn: { label: "OpenVPN (میکروتیک)", icon: ShieldCheck, color: "bg-teal-50 text-teal-600" },
  l2tp: { label: "L2TP/IPsec (میکروتیک)", icon: Lock, color: "bg-amber-50 text-amber-600" },
  ikev2: { label: "IKEv2/IPsec (میکروتیک)", icon: KeyRound, color: "bg-sky-50 text-sky-600" },
  xray: { label: "V2Ray / Xray", icon: Globe, color: "bg-purple-50 text-purple-600" },
};

const FILE_EXT = { wireguard: "conf", openvpn: "txt", l2tp: "txt", ikev2: "txt" };

export default function UserDetail() {
  const { id } = useParams();
  const { isSuperadmin } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [user, setUser] = useState(null);
  const [nodes, setNodes] = useState([]);
  const [admins, setAdmins] = useState([]);
  const [editOpen, setEditOpen] = useState(false);
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
        <div className="text-gray-400">در حال بارگذاری...</div>
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
      setError(err?.response?.data?.detail || "خطا در ذخیره‌سازی");
    } finally {
      setSaving(false);
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
      setError("یک سرور انتخاب کنید");
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
      } else {
        await addXrayConnection(user.id, Number(connNodeId), connFlow);
      }
      setAddConnOpen(false);
      setConnNodeId("");
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || "خطا در افزودن اتصال");
    } finally {
      setSaving(false);
    }
  };

  const removeConnection = async (connId) => {
    if (!confirm("این اتصال حذف شود؟")) return;
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
      setError(err?.response?.data?.detail || "خطا در ذخیره‌سازی");
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
    if (!next && !confirm("این اتصال غیرفعال شود؟ کاربر دیگر نمی‌تواند با این سرویس وصل شود.")) return;
    try {
      await updateConnection(user.id, c.id, { enabled: next });
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || "خطا در تغییر وضعیت اتصال");
    }
  };

  return (
    <Layout>
      <button onClick={() => navigate("/users")} className="flex items-center gap-1 text-sm text-gray-400 hover:text-gray-600 mb-4">
        <ArrowRight size={14} /> بازگشت به لیست کاربران
      </button>

      <Topbar title={user.username} subtitle={user.full_name || "بدون نام کامل"} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
        <div className="card lg:col-span-2">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-bold text-gray-700">مصرف و سهمیه</h3>
            <span className={`badge ${STATUS_STYLES[user.status]}`}>{STATUS_LABELS[user.status]}</span>
          </div>
          <QuotaBar used={user.used_bytes} total={user.total_quota_bytes} />
          <div className="text-xs text-gray-400 mt-2">
            باقی‌مانده: {user.total_quota_bytes ? formatBytes(Math.max(user.total_quota_bytes - user.used_bytes, 0)) : "نامحدود"}
          </div>
          <div className="text-xs text-gray-400 mt-1">
            انقضا:{" "}
            {user.expire_days_after_first_use
              ? `بعد از اولین اتصال فعال می‌شود (${user.expire_days_after_first_use} روز) - هنوز متصل نشده`
              : user.expire_at
              ? formatDateTime(user.expire_at)
              : "بدون انقضا"}
          </div>
          <div className="text-xs text-gray-400 mt-1">
            موجودی اعتبار: {(user.balance || 0).toLocaleString()} تومان
          </div>
          {isSuperadmin && (
            <div className="text-xs text-gray-400 mt-1">
              ادمین مربوطه: {user.owner_admin_username || <span className="text-gray-300">بدون ادمین</span>}
            </div>
          )}
        </div>
        <div className="card flex flex-col justify-center">
          <button className="btn-secondary" onClick={() => setEditOpen(true)}>
            ویرایش سهمیه / انقضا
          </button>
        </div>
      </div>

      <div className="flex items-center justify-between mb-3">
        <h3 className="font-bold text-gray-700">اتصالات</h3>
        <button className="btn-primary" onClick={openAddConn}>
          <Plus size={16} /> افزودن اتصال
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
                    {c.enabled ? "فعال" : "غیرفعال"}
                  </span>
                  <span className={`badge ${c.online ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-400"}`}>
                    <span className={`inline-block w-1.5 h-1.5 rounded-full ml-1 ${c.online ? "bg-emerald-500" : "bg-gray-300"}`} />
                    {c.online ? "آنلاین" : "آفلاین"}
                  </span>
                </div>
              </div>

              <div className="text-xs text-gray-500 space-y-1 mb-3">
                <div>مصرف این اتصال: {formatBytes(c.total_bytes)}</div>
                {c.type === "xray" && <div className="truncate">شناسه: {c.xr_email}</div>}
                {c.type === "wireguard" && <div>آدرس داخلی: {c.wg_client_address}</div>}
                {(c.type === "openvpn" || c.type === "l2tp" || c.type === "ikev2") && (
                  <>
                    <div>نام کاربری: {c.ppp_username}</div>
                    <div>
                      اتصال هم‌زمان: {c.active_session_count ?? 0} /{" "}
                      {c.max_concurrent_sessions ? c.max_concurrent_sessions : "نامحدود"}
                    </div>
                    {c.banned_until && new Date(c.banned_until) > new Date() && (
                      <div className="text-red-500">
                        بن موقت تا {formatDateTime(c.banned_until)}{" "}
                        <button className="underline" onClick={() => unban(c)}>
                          رفع بن
                        </button>
                      </div>
                    )}
                  </>
                )}
                <div>تاریخ ساخت: {formatDateTime(c.created_at)}</div>
              </div>

              <div className="flex gap-2">
                <button className="btn-secondary flex-1" onClick={() => showShare(c.id)}>
                  <QrCode size={14} /> دریافت کانفیگ
                </button>
                {(c.type === "openvpn" || c.type === "l2tp" || c.type === "ikev2") && (
                  <button className="btn-secondary" title="محدودیت اتصال هم‌زمان" onClick={() => openLimit(c)}>
                    <ShieldCheck size={14} />
                  </button>
                )}
                <button
                  className="btn-secondary"
                  title={c.enabled ? "غیرفعال کردن اتصال" : "فعال کردن اتصال"}
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
          <div className="card text-center text-gray-400 col-span-2 py-10">هنوز اتصالی برای این کاربر ثبت نشده است</div>
        )}
      </div>

      {/* Edit modal */}
      <Modal open={editOpen} onClose={() => setEditOpen(false)} title="ویرایش کاربر">
        <form onSubmit={saveEdit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">نام کامل</label>
            <input className="input" value={editForm.full_name} onChange={(e) => setEditForm({ ...editForm, full_name: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">حجم مجاز (گیگابایت)</label>
              <input type="number" step="0.1" min="0" className="input" placeholder="0 = نامحدود" value={editForm.quota_gb} onChange={(e) => setEditForm({ ...editForm, quota_gb: e.target.value })} />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">تعداد اتصال هم‌زمان (کل سرویس‌های کاربر - OpenVPN/L2TP/WireGuard/Xray)</label>
              <input
                type="number"
                min="0"
                className="input"
                placeholder="بدون تغییر"
                value={editForm.max_concurrent_sessions}
                onChange={(e) => setEditForm({ ...editForm, max_concurrent_sessions: e.target.value })}
              />
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">موجودی اعتبار (تومان)</label>
            <input
              type="number"
              className="input"
              value={editForm.balance}
              onChange={(e) => setEditForm({ ...editForm, balance: e.target.value })}
            />
          </div>

          {isSuperadmin && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">متعلق به ادمین</label>
              <select
                className="input"
                value={editForm.owner_admin_id}
                onChange={(e) => setEditForm({ ...editForm, owner_admin_id: e.target.value })}
              >
                <option value="">بدون ادمین (فقط ادمین اصلی می‌بیند)</option>
                {admins.filter((a) => !a.is_superadmin).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.username}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div>
            <label className="block text-sm text-gray-600 mb-1">آیدی عددی تلگرام (برای اتصال دستی به ربات)</label>
            <input
              type="number"
              className="input"
              placeholder="خالی = وصل نیست"
              dir="ltr"
              value={editForm.telegram_id}
              onChange={(e) => setEditForm({ ...editForm, telegram_id: e.target.value })}
            />
            <div className="text-xs text-gray-400 mt-1">
              اگه مشتری قبلاً با این پنل حسابی نداشته، می‌تونی آیدی عددی تلگرامش رو اینجا دستی وصل کنی تا از طریق ربات به این حساب دسترسی داشته باشد.
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">نوع انقضا</label>
            <select
              className="input"
              value={editForm.expire_mode}
              onChange={(e) => setEditForm({ ...editForm, expire_mode: e.target.value })}
            >
              <option value="none">بدون انقضا</option>
              <option value="date">تاریخ مشخص</option>
              <option value="days_from_now">تعداد روز از الان</option>
              <option value="first_use">تعداد روز از اولین اتصال</option>
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
                placeholder="مثلا 30"
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
                  placeholder="مثلا 30"
                  value={editForm.expire_days}
                  onChange={(e) => setEditForm({ ...editForm, expire_days: e.target.value })}
                />
                <div className="text-xs text-gray-400 mt-1">
                  تا وقتی کاربر برای اولین بار وصل نشده انقضا فعال نمی‌شود؛ از لحظه اولین اتصال موفق، شمارش {editForm.expire_days || "N"} روز شروع می‌شود.
                </div>
              </>
            )}
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">یادداشت</label>
            <textarea className="input" rows={2} value={editForm.notes} onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })} />
          </div>
          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setEditOpen(false)}>
              انصراف
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              <Save size={16} /> ذخیره
            </button>
          </div>
        </form>
      </Modal>

      {/* Add connection modal */}
      <Modal open={addConnOpen} onClose={() => setAddConnOpen(false)} title="افزودن اتصال جدید">
        <div className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">سرور</label>
            <select className="input" value={connNodeId} onChange={(e) => setConnNodeId(e.target.value)}>
              <option value="">انتخاب کنید...</option>
              {nodes.map((n) => (
                <option key={n.id} value={n.id}>
                  {n.name} ({n.type === "mikrotik" ? "میکروتیک" : "V2Ray/Xray"})
                </option>
              ))}
            </select>
          </div>
          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}

          {selectedNode?.type === "mikrotik" && (
            <>
              <div>
                <label className="block text-sm text-gray-600 mb-1">
                  حداکثر اتصال هم‌زمان (فقط OpenVPN/L2TP/IKEv2)
                </label>
                <input
                  type="number"
                  min="0"
                  className="input"
                  placeholder="0 = نامحدود، پیش‌فرض 1"
                  value={connMaxSessions}
                  onChange={(e) => setConnMaxSessions(e.target.value)}
                />
              </div>
              <div className="grid grid-cols-4 gap-2">
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
              </div>
            </>
          )}

          {selectedNode?.type === "xray" && (
            <button disabled={saving} className="btn-primary w-full" onClick={() => addConnection("xray")}>
              <Globe size={16} /> افزودن V2Ray
            </button>
          )}

          {!selectedNode && <div className="text-xs text-gray-400">بعد از انتخاب سرور، نوع اتصال نمایش داده می‌شود.</div>}
        </div>
      </Modal>

      {/* Concurrent-session limit modal */}
      <Modal open={!!limitConn} onClose={() => setLimitConn(null)} title="محدودیت اتصال هم‌زمان">
        {limitConn && (
          <div className="space-y-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">حداکثر تعداد اتصال هم‌زمان</label>
              <input
                type="number"
                min="0"
                className="input"
                placeholder="0 = نامحدود"
                value={limitValue}
                onChange={(e) => setLimitValue(e.target.value)}
              />
              <div className="text-xs text-gray-400 mt-1">
                اگه کاربر بیشتر از این تعداد و به‌طور مکرر (۵ بار در ۱ دقیقه) تلاش کنه هم‌زمان وصل بشه، به‌مدت ۳ دقیقه
                موقتا بن می‌شه.
              </div>
            </div>
            {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
            <div className="flex justify-end gap-2 pt-2">
              <button type="button" className="btn-secondary" onClick={() => setLimitConn(null)}>
                انصراف
              </button>
              <button type="button" disabled={saving} className="btn-primary" onClick={saveLimit}>
                ذخیره
              </button>
            </div>
          </div>
        )}
      </Modal>

      {/* Share modal */}
      <Modal open={!!shareData} onClose={closeShare} title="کانفیگ اتصال">
        {shareData && (
          <div className="space-y-4">
            {qrUrl && (
              <div className="flex flex-col items-center gap-2">
                <img src={qrUrl} alt="QR Code" className="rounded-xl border border-gray-100" width={200} height={200} />
                <div className="text-xs text-gray-400">با اسکن این QR هم می‌توانید در اکثر اپ‌های کلاینت وصل شوید</div>
              </div>
            )}

            {shareData.link && (
              <div>
                <div className="text-xs text-gray-500 mb-1">لینک اشتراک‌گذاری</div>
                <div className="flex gap-2">
                  <input readOnly className="input font-mono text-xs" value={shareData.link} />
                  <button className="btn-secondary" onClick={() => onCopy("link", shareData.link)}>
                    {copiedKey === "link" ? <Check size={14} /> : <Copy size={14} />}
                  </button>
                </div>
                {copiedKey === "link" && <div className="text-xs text-emerald-600 mt-1">کپی شد</div>}
                {copiedKey === "link-failed" && <div className="text-xs text-red-500 mt-1">کپی خودکار پشتیبانی نشد؛ متن را دستی انتخاب و کپی کنید</div>}
              </div>
            )}

            {shareData.config_text && (
              <div>
                <div className="text-xs text-gray-500 mb-1">
                  {shareData.kind === "wireguard" ? "فایل کانفیگ WireGuard (.conf)" : "اطلاعات اتصال (یوزر/پسورد)"}
                </div>
                <textarea readOnly className="input font-mono text-xs" rows={9} value={shareData.config_text} />
                <div className="flex gap-2 mt-2">
                  <button className="btn-secondary flex-1" onClick={() => onCopy("config", shareData.config_text)}>
                    {copiedKey === "config" ? <Check size={14} /> : <Copy size={14} />} کپی
                  </button>
                  <button className="btn-primary flex-1" onClick={onDownload}>
                    <Download size={14} /> دانلود فایل
                  </button>
                </div>
                {copiedKey === "config" && <div className="text-xs text-emerald-600 mt-1">کپی شد</div>}
                {copiedKey === "config-failed" && <div className="text-xs text-red-500 mt-1">کپی خودکار پشتیبانی نشد؛ متن را دستی انتخاب و کپی کنید</div>}
              </div>
            )}
          </div>
        )}
      </Modal>
    </Layout>
  );
}
