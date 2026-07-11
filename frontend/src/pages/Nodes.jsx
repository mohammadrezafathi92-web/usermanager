import React, { useEffect, useState } from "react";
import { Plus, Trash2, Pencil, Wifi, Globe, PlugZap, CheckCircle2, XCircle, Power } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import { fetchNodes, createNode, updateNode, deleteNode, testNode, pushRadiusConfig, importPppUsers, importUserManagerUsers, import3xuiClients } from "../api/client.js";
import { formatDateTime } from "../utils.js";

const emptyForm = {
  name: "",
  type: "mikrotik",
  mt_host: "",
  mt_port: 8728,
  mt_api_ssl_port: 8729,
  mt_username: "",
  mt_password: "",
  mt_use_ssl: false,
  mt_endpoint_host: "",
  mt_wireguard_interface: "wireguard1",
  mt_endpoint_port: 13231,
  mt_client_subnet: "10.66.66.0/24",
  mt_client_dns: "1.1.1.1",
  mt_radius_secret: "",
  mt_ovpn_port: 1194,
  mt_ovpn_certificate: "",
  mt_l2tp_use_ipsec: true,
  mt_l2tp_ipsec_secret: "",
  mt_ikev2_psk: "",
  xr_panel_mode: "ssh",
  xr_panel_base_url: "",
  xr_panel_api_token: "",
  xr_panel_username: "",
  xr_panel_password: "",
  xr_panel_inbound_id: null,
  xr_ssh_host: "",
  xr_ssh_port: 22,
  xr_ssh_username: "root",
  xr_ssh_password: "",
  xr_config_path: "/usr/local/etc/xray/config.json",
  xr_service_name: "xray",
  xr_api_address: "127.0.0.1:10085",
  xr_inbound_tag: "proxy",
  xr_public_host: "",
  xr_public_port: 443,
  xr_network: "tcp",
  xr_security: "tls",
  xr_sni: "",
};

export default function Nodes() {
  const [nodes, setNodes] = useState([]);
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState({});
  const [radiusPanelHost, setRadiusPanelHost] = useState("");
  const [radiusInterimUpdate, setRadiusInterimUpdate] = useState("00:01:00");
  const [radiusStatus, setRadiusStatus] = useState("");
  const [importStatus, setImportStatus] = useState("");
  const [importResult, setImportResult] = useState(null);
  const [umImportStatus, setUmImportStatus] = useState("");
  const [umImportResult, setUmImportResult] = useState(null);
  const [xuiImportStatus, setXuiImportStatus] = useState("");
  const [xuiImportResult, setXuiImportResult] = useState(null);
  const [togglingId, setTogglingId] = useState(null);

  const load = () => fetchNodes().then((res) => setNodes(res.data));
  useEffect(() => {
    load();
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  // Shared by openCreate/openEdit so a leftover import/RADIUS result from a
  // previous edit session can never leak into a later create/edit modal -
  // previously only openEdit reset these, so closing an edit right after an
  // import and then clicking "افزودن نود" showed the old node's stale
  // result until an import button was clicked again.
  const resetModalStatus = () => {
    setError("");
    setRadiusStatus("");
    setImportStatus("");
    setImportResult(null);
    setUmImportStatus("");
    setUmImportResult(null);
    setXuiImportStatus("");
    setXuiImportResult(null);
  };

  const openCreate = () => {
    setEditingId(null);
    setForm(emptyForm);
    resetModalStatus();
    setOpen(true);
  };

  const openEdit = (node) => {
    setEditingId(node.id);
    setForm({ ...emptyForm, ...node });
    resetModalStatus();
    setOpen(true);
  };

  const onImportPpp = async () => {
    if (!editingId) return;
    if (!confirm("لیست PPP secret های موجود روی این روتر خونده بشه و کاربرهایی که تو پنل نیستن اضافه بشن؟ چیزی روی روتر تغییر نمی‌کنه.")) return;
    setImportStatus("loading");
    setImportResult(null);
    try {
      const res = await importPppUsers(editingId);
      setImportResult(res.data);
      setImportStatus("done");
    } catch (err) {
      setImportStatus(err?.response?.data?.detail || "خطا در ایمپورت");
    }
  };

  const onImportUm = async () => {
    if (!editingId) return;
    if (!confirm("کاربران، حجم/اعتبار فعال و مصرف قبلی از User Manager خود میکروتیک خونده بشه؟ چیزی روی روتر تغییر نمی‌کنه.")) return;
    setUmImportStatus("loading");
    setUmImportResult(null);
    try {
      const res = await importUserManagerUsers(editingId);
      setUmImportResult(res.data);
      setUmImportStatus("done");
    } catch (err) {
      setUmImportStatus(err?.response?.data?.detail || "خطا در ایمپورت");
    }
  };

  const onImportXui = async () => {
    if (!editingId) return;
    if (!confirm("کلاینت‌های موجود روی اینباند پنل 3X-UI خونده بشن و وارد پنل بشن؟ چیزی روی 3X-UI تغییر نمی‌کنه.")) return;
    setXuiImportStatus("loading");
    setXuiImportResult(null);
    try {
      const res = await import3xuiClients(editingId);
      setXuiImportResult(res.data);
      setXuiImportStatus("done");
    } catch (err) {
      setXuiImportStatus(err?.response?.data?.detail || "خطا در ایمپورت");
    }
  };

  const onPushRadius = async () => {
    if (!editingId) return;
    setRadiusStatus("loading");
    try {
      const res = await pushRadiusConfig(editingId, radiusPanelHost, radiusInterimUpdate);
      setRadiusStatus(res?.data?.message || "با موفقیت اعمال شد");
    } catch (err) {
      setRadiusStatus(err?.response?.data?.detail || "خطا در اعمال تنظیمات RADIUS");
    }
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      if (editingId) {
        await updateNode(editingId, form);
      } else {
        await createNode(form);
      }
      setOpen(false);
      setForm(emptyForm);
      setEditingId(null);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || "خطا در ذخیره سرور");
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (id) => {
    if (!confirm("این سرور حذف شود؟")) return;
    try {
      await deleteNode(id);
      load();
    } catch (err) {
      alert(err?.response?.data?.detail || "خطا در حذف");
    }
  };

  const onToggleEnabled = async (node) => {
    const next = !node.enabled;
    if (!next && !confirm(`سرور «${node.name}» غیرفعال بشه؟ دیگه پول (poll) نمی‌شه، وضعیت آنلاین/مصرفش به‌روز نمی‌مونه و از لیست سرورهای در دسترس ربات حذف می‌شه (کانکشن‌های موجودش پاک نمی‌شن).`)) {
      return;
    }
    setTogglingId(node.id);
    // Optimistic update so the toggle feels instant; reconciled by load() below.
    setNodes((ns) => ns.map((n) => (n.id === node.id ? { ...n, enabled: next } : n)));
    try {
      await updateNode(node.id, { enabled: next });
    } catch (err) {
      alert(err?.response?.data?.detail || "خطا در تغییر وضعیت سرور");
      setNodes((ns) => ns.map((n) => (n.id === node.id ? { ...n, enabled: node.enabled } : n)));
    } finally {
      setTogglingId(null);
    }
  };

  const onTest = async (id) => {
    setTestResult((r) => ({ ...r, [id]: "loading" }));
    try {
      await testNode(id);
      setTestResult((r) => ({ ...r, [id]: "ok" }));
    } catch (err) {
      setTestResult((r) => ({ ...r, [id]: err?.response?.data?.detail || "خطا" }));
    }
  };

  return (
    <Layout>
      <Topbar title="سرورها" subtitle="مدیریت روترهای میکروتیک (WireGuard/OpenVPN/L2TP/IKEv2) و سرورهای V2Ray/Xray" />

      <div className="flex justify-end mb-4">
        <button className="btn-primary" onClick={openCreate}>
          <Plus size={16} /> افزودن سرور
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {nodes.map((n) => (
          <div key={n.id} className={`card ${!n.enabled ? "opacity-60" : ""}`}>
            <div className="flex items-start justify-between mb-3">
              <div className="flex items-center gap-2">
                <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${n.type === "mikrotik" ? "bg-indigo-50 text-indigo-600" : "bg-purple-50 text-purple-600"}`}>
                  {n.type === "mikrotik" ? <Wifi size={18} /> : <Globe size={18} />}
                </div>
                <div>
                  <div className="font-medium text-gray-800 flex items-center gap-2">
                    {n.name}
                    <span className={`badge ${n.enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                      {n.enabled ? "فعال" : "غیرفعال"}
                    </span>
                  </div>
                  <div className="text-xs text-gray-400">{n.type === "mikrotik" ? "میکروتیک (WireGuard/OpenVPN/L2TP/IKEv2)" : "V2Ray / Xray"}</div>
                </div>
              </div>
              <button
                type="button"
                title={n.enabled ? "غیرفعال کردن سرور" : "فعال کردن سرور"}
                disabled={togglingId === n.id}
                onClick={() => onToggleEnabled(n)}
                className={`w-9 h-9 rounded-xl flex items-center justify-center transition-colors disabled:opacity-50 ${
                  n.enabled ? "bg-emerald-50 text-emerald-600 hover:bg-emerald-100" : "bg-gray-100 text-gray-400 hover:bg-gray-200"
                }`}
              >
                <Power size={16} />
              </button>
            </div>

            <div className="text-xs text-gray-500 space-y-1 mb-3">
              <div>آدرس: {n.type === "mikrotik" ? `${n.mt_host}:${n.mt_use_ssl ? n.mt_api_ssl_port : n.mt_port}${n.mt_use_ssl ? " (SSL)" : ""}` : (n.xr_panel_mode === "3xui" ? `${n.xr_panel_base_url} (پنل 3X-UI)` : n.xr_ssh_host)}</div>
              <div>آخرین اتصال موفق: {formatDateTime(n.last_seen)}</div>
              {n.last_error && <div className="text-red-500">خطا: {n.last_error}</div>}
              {!n.enabled && <div className="text-amber-600">این سرور غیرفعاله - پول نمی‌شه و برای اتصال جدید در دسترس نیست.</div>}
            </div>

            <div className="flex items-center gap-2">
              <button className="btn-secondary flex-1" onClick={() => onTest(n.id)}>
                <PlugZap size={14} /> تست اتصال
              </button>
              <button className="btn-secondary" onClick={() => openEdit(n)}>
                <Pencil size={14} />
              </button>
              <button className="btn-danger" onClick={() => onDelete(n.id)}>
                <Trash2 size={14} />
              </button>
            </div>
            {testResult[n.id] && testResult[n.id] !== "loading" && (
              <div className={`flex items-center gap-1 text-xs mt-2 ${testResult[n.id] === "ok" ? "text-emerald-600" : "text-red-500"}`}>
                {testResult[n.id] === "ok" ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                {testResult[n.id] === "ok" ? "اتصال موفق بود" : testResult[n.id]}
              </div>
            )}
          </div>
        ))}
        {nodes.length === 0 && <div className="card text-center text-gray-400 col-span-2 py-10">هنوز سروری اضافه نشده است</div>}
      </div>

      <Modal open={open} onClose={() => setOpen(false)} title={editingId ? "ویرایش سرور" : "افزودن سرور جدید"} width="max-w-2xl">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">نام سرور *</label>
            <input className="input" required value={form.name} onChange={(e) => set("name", e.target.value)} />
          </div>

          <div className="flex gap-2">
            <button type="button" disabled={!!editingId} onClick={() => set("type", "mikrotik")} className={`flex-1 rounded-xl border py-2 text-sm font-medium disabled:opacity-60 ${form.type === "mikrotik" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-gray-200 text-gray-500"}`}>
              میکروتیک (WireGuard/OpenVPN/L2TP/IKEv2)
            </button>
            <button type="button" disabled={!!editingId} onClick={() => set("type", "xray")} className={`flex-1 rounded-xl border py-2 text-sm font-medium disabled:opacity-60 ${form.type === "xray" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-gray-200 text-gray-500"}`}>
              V2Ray / Xray
            </button>
          </div>

          {form.type === "mikrotik" ? (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-gray-600 mb-1">آدرس IP روتر *</label>
                  <input className="input" required value={form.mt_host} onChange={(e) => set("mt_host", e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">پورت API</label>
                  <input type="number" className="input" value={form.mt_port} onChange={(e) => set("mt_port", Number(e.target.value))} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">نام کاربری *</label>
                  <input className="input" required value={form.mt_username} onChange={(e) => set("mt_username", e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">رمز عبور *</label>
                  <input type="password" className="input" required value={form.mt_password} onChange={(e) => set("mt_password", e.target.value)} />
                </div>
                <div className="col-span-2">
                  <label className="block text-sm text-gray-600 mb-1">آدرس عمومی روتر (Endpoint) *</label>
                  <input className="input" required placeholder="مثلا 1.2.3.4 یا vpn.example.com" value={form.mt_endpoint_host} onChange={(e) => set("mt_endpoint_host", e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">پورت API-SSL</label>
                  <input type="number" className="input" value={form.mt_api_ssl_port} onChange={(e) => set("mt_api_ssl_port", Number(e.target.value))} />
                </div>
                <div className="col-span-2 flex items-center gap-2">
                  <input type="checkbox" id="mt_ssl" checked={form.mt_use_ssl} onChange={(e) => set("mt_use_ssl", e.target.checked)} />
                  <label htmlFor="mt_ssl" className="text-sm text-gray-600">استفاده از API-SSL (به‌جای پورت API معمولی از پورت API-SSL بالا استفاده می‌شود)</label>
                </div>
              </div>

              <div className="border-t border-gray-100 pt-3">
                <div className="text-sm font-medium text-gray-700 mb-2">تنظیمات WireGuard</div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">نام اینترفیس</label>
                    <input className="input" value={form.mt_wireguard_interface} onChange={(e) => set("mt_wireguard_interface", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">پورت WireGuard</label>
                    <input type="number" className="input" value={form.mt_endpoint_port} onChange={(e) => set("mt_endpoint_port", Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">رنج IP کلاینت‌ها</label>
                    <input className="input" value={form.mt_client_subnet} onChange={(e) => set("mt_client_subnet", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">DNS کلاینت</label>
                    <input className="input" value={form.mt_client_dns} onChange={(e) => set("mt_client_dns", e.target.value)} />
                  </div>
                </div>
              </div>

              <div className="border-t border-gray-100 pt-3">
                <div className="text-sm font-medium text-gray-700 mb-2">OpenVPN / L2TP / IKEv2 (احراز هویت با RADIUS)</div>
                <p className="text-xs text-gray-400 mb-3">
                  پنل فقط یوزرنیم/پسورد می‌سازد و از طریق RADIUS خودش آن‌ها را تایید می‌کند. IP pool، سرور
                  OpenVPN/L2TP/IKEv2، سرتیفیکیت و IPsec را خودتان مستقیما روی میکروتیک تنظیم کنید. مقادیر پورت/سرتیفیکیت
                  زیر فقط برای نمایش صحیح به کاربر نهایی ذخیره می‌شوند و باید دقیقا با روتر یکی باشند.
                </p>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">RADIUS Secret</label>
                    <input className="input" placeholder="یک رشته تصادفی و مشترک با روتر" value={form.mt_radius_secret} onChange={(e) => set("mt_radius_secret", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">پورت OpenVPN (که روی روتر تنظیم کرده‌اید)</label>
                    <input type="number" className="input" value={form.mt_ovpn_port} onChange={(e) => set("mt_ovpn_port", Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">نام سرتیفیکیت OpenVPN (فقط جهت اطلاع)</label>
                    <input className="input" placeholder="مثلا server-cert" value={form.mt_ovpn_certificate} onChange={(e) => set("mt_ovpn_certificate", e.target.value)} />
                  </div>
                  <div className="col-span-2 flex items-center gap-2">
                    <input type="checkbox" id="l2tp_ipsec" checked={form.mt_l2tp_use_ipsec} onChange={(e) => set("mt_l2tp_use_ipsec", e.target.checked)} />
                    <label htmlFor="l2tp_ipsec" className="text-sm text-gray-600">L2TP روی روتر با IPsec تنظیم شده</label>
                  </div>
                  {form.mt_l2tp_use_ipsec && (
                    <div className="col-span-2">
                      <label className="block text-sm text-gray-600 mb-1">کلید IPsec (PSK که روی روتر تنظیم کرده‌اید)</label>
                      <input className="input" value={form.mt_l2tp_ipsec_secret} onChange={(e) => set("mt_l2tp_ipsec_secret", e.target.value)} />
                    </div>
                  )}
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">کلید IPsec برای IKEv2 (PSK، اختیاری - فقط جهت اطلاع به کاربر)</label>
                    <input className="input" placeholder="در صورت استفاده از احراز هویت PSK روی روتر" value={form.mt_ikev2_psk} onChange={(e) => set("mt_ikev2_psk", e.target.value)} />
                  </div>
                </div>

                {editingId && (
                  <div className="mt-3 bg-gray-50 rounded-lg p-3">
                    <div className="text-xs text-gray-500 mb-2">
                      با دکمه زیر، پنل با استفاده از همان اتصال API میکروتیک، این سرور را به عنوان کلاینت RADIUS
                      (/radius) روی روتر ثبت و <span dir="ltr">ppp aaa use-radius=yes</span> را فعال می‌کند. اول باید
                      RADIUS Secret بالا را ذخیره کرده باشید.
                    </div>
                    <div className="mb-2">
                      <label className="block text-xs text-gray-500 mb-1">آدرس این سرور User Manager (که روتر باید بهش وصل شود)</label>
                      <input
                        className="input w-full"
                        placeholder="مثلا 1.2.3.4"
                        value={radiusPanelHost}
                        onChange={(e) => setRadiusPanelHost(e.target.value)}
                      />
                    </div>
                    <div className="mb-3">
                      <label className="block text-xs text-gray-500 mb-1">
                        فاصله بروزرسانی مصرف (Interim-Update) - فرمت ساعت:دقیقه:ثانیه
                      </label>
                      <input
                        className="input w-full"
                        placeholder="00:01:00"
                        title="هرچی کمتر باشه مصرف لحظه‌ای دقیق‌تر نمایش داده می‌شه"
                        value={radiusInterimUpdate}
                        onChange={(e) => setRadiusInterimUpdate(e.target.value)}
                      />
                      <div className="text-xs text-gray-400 mt-1">
                        مثلا 00:01:00 یعنی هر ۱ دقیقه مصرف لحظه‌ای کاربر متصل به‌روز می‌شود.
                      </div>
                    </div>
                    <button type="button" className="btn-secondary w-full" onClick={onPushRadius}>
                      اعمال RADIUS روی روتر
                    </button>
                    {radiusStatus && radiusStatus !== "loading" && (
                      <div className="text-xs mt-2 text-gray-600">{radiusStatus}</div>
                    )}
                  </div>
                )}

                {editingId && (
                  <div className="mt-3 bg-gray-50 rounded-lg p-3">
                    <div className="text-xs text-gray-500 mb-2">
                      اگه قبل از پنل، مستقیم روی این روتر یوزر OpenVPN/L2TP (PPP secret) ساخته بودید، با دکمه زیر
                      همه‌شون رو (با همون یوزر/پسورد) وارد پنل کنید. فقط خونده می‌شه؛ چیزی روی روتر تغییر نمی‌کنه و
                      یوزرهایی که قبلا ایمپورت شدن دوباره اضافه نمی‌شن.
                    </div>
                    <button type="button" className="btn-secondary" onClick={onImportPpp} disabled={importStatus === "loading"}>
                      {importStatus === "loading" ? "در حال خواندن..." : "ایمپورت کاربران قبلی PPP از روتر"}
                    </button>
                    {importResult && (
                      <div className="text-xs mt-2 text-gray-600">
                        {importResult.imported_count} کاربر اضافه شد، {importResult.skipped_count} رد شد.
                        {importResult.skipped_count > 0 && (
                          <ul className="mt-1 list-disc pr-4 space-y-0.5 max-h-32 overflow-y-auto">
                            {importResult.skipped.map((s, i) => (
                              <li key={i}>{s.name}: {s.reason}</li>
                            ))}
                          </ul>
                        )}
                      </div>
                    )}
                    {typeof importStatus === "string" && importStatus !== "loading" && importStatus !== "done" && (
                      <div className="text-xs mt-2 text-red-500">{importStatus}</div>
                    )}
                  </div>
                )}

                {editingId && (
                  <div className="mt-3 bg-gray-50 rounded-lg p-3">
                    <div className="text-xs text-gray-500 mb-2">
                      اگه کاربرهاتون رو با <span dir="ltr">User Manager</span> خودِ میکروتیک (نه فقط PPP secret ساده)
                      با حجم/تاریخ انقضا مدیریت می‌کردید، از این دکمه استفاده کنید. حجم فعلی، اعتبار فعال، مصرف قبلی
                      و محدودیت «تعداد اتصال هم‌زمان» (shared-users) هم از روتر خونده و به پنل منتقل می‌شه. فقط خونده
                      می‌شه؛ چیزی روی روتر تغییر نمی‌کنه.
                    </div>
                    <button type="button" className="btn-secondary" onClick={onImportUm} disabled={umImportStatus === "loading"}>
                      {umImportStatus === "loading" ? "در حال خواندن..." : "ایمپورت از User Manager میکروتیک"}
                    </button>
                    {umImportResult && (
                      <div className="text-xs mt-2 text-gray-600">
                        {umImportResult.imported_count} کاربر اضافه شد، {umImportResult.skipped_count} رد شد.
                        {umImportResult.skipped_count > 0 && (
                          <ul className="mt-1 list-disc pr-4 space-y-0.5 max-h-32 overflow-y-auto">
                            {umImportResult.skipped.map((s, i) => (
                              <li key={i}>{s.name}: {s.reason}</li>
                            ))}
                          </ul>
                        )}
                      </div>
                    )}
                    {typeof umImportStatus === "string" && umImportStatus !== "loading" && umImportStatus !== "done" && (
                      <div className="text-xs mt-2 text-red-500">{umImportStatus}</div>
                    )}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              <div>
                <label className="block text-sm text-gray-600 mb-1">روش اتصال</label>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => set("xr_panel_mode", "ssh")}
                    className={`flex-1 rounded-xl border py-2 text-sm font-medium ${form.xr_panel_mode !== "3xui" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-gray-200 text-gray-500"}`}
                  >
                    SSH (دسترسی مستقیم به سرور)
                  </button>
                  <button
                    type="button"
                    onClick={() => set("xr_panel_mode", "3xui")}
                    className={`flex-1 rounded-xl border py-2 text-sm font-medium ${form.xr_panel_mode === "3xui" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-gray-200 text-gray-500"}`}
                  >
                    پنل 3X-UI (بدون SSH)
                  </button>
                </div>
                {form.xr_panel_mode === "3xui" && (
                  <p className="text-xs text-gray-400 mt-1">
                    برای مواردی که Xray/3X-UI روی جایی نصب شده که SSH بهش دسترسی ندارید (مثلا کانتینر میکروتیک) - پنل مستقیم از طریق آدرس وب خودش مدیریت می‌شود.
                  </p>
                )}
              </div>

              {form.xr_panel_mode === "3xui" ? (
                <div className="grid grid-cols-2 gap-3">
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">آدرس پنل (شامل مسیر امنیتی در صورت وجود) *</label>
                    <input
                      className="input"
                      placeholder="http://1.2.3.4:2053/xyzpanel"
                      required
                      value={form.xr_panel_base_url}
                      onChange={(e) => set("xr_panel_base_url", e.target.value)}
                    />
                  </div>
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">API Token (ترجیحی - از Settings ← Authentication ← API Token در خود پنل)</label>
                    <input
                      className="input"
                      placeholder="اگه پر بشه، دیگه به یوزر/پسورد پایین نیازی نیست"
                      value={form.xr_panel_api_token}
                      onChange={(e) => set("xr_panel_api_token", e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">یوزر پنل (اگه API Token نداری)</label>
                    <input className="input" value={form.xr_panel_username} onChange={(e) => set("xr_panel_username", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">پسورد پنل (اگه API Token نداری)</label>
                    <input type="password" className="input" value={form.xr_panel_password} onChange={(e) => set("xr_panel_password", e.target.value)} />
                  </div>
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">شناسه (ID) اینباند در پنل 3X-UI</label>
                    <input
                      type="number"
                      className="input"
                      placeholder="از صفحه ویرایش اینباند در 3X-UI قابل مشاهده است"
                      value={form.xr_panel_inbound_id ?? ""}
                      onChange={(e) => set("xr_panel_inbound_id", e.target.value ? Number(e.target.value) : null)}
                    />
                  </div>
                  {editingId && (
                    <div className="col-span-2 mt-1 bg-gray-50 rounded-lg p-3">
                      <div className="text-xs text-gray-500 mb-2">
                        اگه قبل از وصل کردن این نود، مستقیم توی خودِ پنل 3X-UI کلاینت ساخته بودید، با دکمه زیر همه‌شون
                        رو (با همون uuid/لینک قبلی) وارد پنل کنید. فقط خونده می‌شه؛ چیزی روی 3X-UI تغییر نمی‌کنه و
                        کلاینت‌هایی که قبلا ایمپورت شدن دوباره اضافه نمی‌شن.
                      </div>
                      <button type="button" className="btn-secondary" onClick={onImportXui} disabled={xuiImportStatus === "loading"}>
                        {xuiImportStatus === "loading" ? "در حال خواندن..." : "ایمپورت کلاینت‌های قبلی 3X-UI"}
                      </button>
                      {xuiImportResult && (
                        <div className="text-xs mt-2 text-gray-600">
                          {xuiImportResult.imported_count} کاربر اضافه شد، {xuiImportResult.skipped_count} رد شد.
                          {xuiImportResult.skipped_count > 0 && (
                            <ul className="mt-1 list-disc pr-4 space-y-0.5 max-h-32 overflow-y-auto">
                              {xuiImportResult.skipped.map((s, i) => (
                                <li key={i}>{s.name}: {s.reason}</li>
                              ))}
                            </ul>
                          )}
                        </div>
                      )}
                      {typeof xuiImportStatus === "string" && xuiImportStatus !== "loading" && xuiImportStatus !== "done" && (
                        <div className="text-xs mt-2 text-red-500">{xuiImportStatus}</div>
                      )}
                    </div>
                  )}
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">آدرس سرور (SSH) *</label>
                    <input className="input" required value={form.xr_ssh_host} onChange={(e) => set("xr_ssh_host", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">پورت SSH</label>
                    <input type="number" className="input" value={form.xr_ssh_port} onChange={(e) => set("xr_ssh_port", Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">یوزر SSH</label>
                    <input className="input" value={form.xr_ssh_username} onChange={(e) => set("xr_ssh_username", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">پسورد SSH</label>
                    <input type="password" className="input" value={form.xr_ssh_password} onChange={(e) => set("xr_ssh_password", e.target.value)} />
                  </div>
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">مسیر config.json</label>
                    <input className="input" value={form.xr_config_path} onChange={(e) => set("xr_config_path", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">نام سرویس systemd</label>
                    <input className="input" value={form.xr_service_name} onChange={(e) => set("xr_service_name", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">آدرس API آماری</label>
                    <input className="input" value={form.xr_api_address} onChange={(e) => set("xr_api_address", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">تگ اینباند</label>
                    <input className="input" value={form.xr_inbound_tag} onChange={(e) => set("xr_inbound_tag", e.target.value)} />
                  </div>
                </div>
              )}

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-gray-600 mb-1">دامنه/آی‌پی عمومی برای کلاینت</label>
                  <input className="input" value={form.xr_public_host} onChange={(e) => set("xr_public_host", e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">پورت عمومی</label>
                  <input type="number" className="input" value={form.xr_public_port} onChange={(e) => set("xr_public_port", Number(e.target.value))} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">SNI</label>
                  <input className="input" value={form.xr_sni} onChange={(e) => set("xr_sni", e.target.value)} />
                </div>
              </div>
            </div>
          )}

          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              انصراف
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? "در حال ذخیره..." : "ذخیره سرور"}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
