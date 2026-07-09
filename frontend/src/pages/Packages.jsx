import React, { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, Power, Package as PackageIcon, Server, Paperclip, Download } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import {
  fetchPackages,
  createPackage,
  updatePackage,
  deletePackage,
  fetchNodes,
  uploadPackageFile,
  deletePackageFile,
} from "../api/client.js";

const emptyForm = {
  name: "",
  quota_gb: 20,
  duration_days: 30,
  price: 0,
  cooperation_price: "",
  description: "",
  enabled: true,
  sort_order: 0,
  max_concurrent_sessions: "",
  custom_message: "",
  connections: [],
};

function formatFileSize(bytes) {
  if (!bytes) return "0 KB";
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(0)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

const emptyConn = { node_id: "", protocol: "xray", flow: "" };

const PROTOCOL_LABELS = { wireguard: "WireGuard", openvpn: "OpenVPN", l2tp: "L2TP", ikev2: "IKEv2", xray: "V2Ray/Xray" };

function formatToman(n) {
  return new Intl.NumberFormat("fa-IR").format(n || 0);
}

export default function Packages() {
  const [items, setItems] = useState([]);
  const [nodes, setNodes] = useState([]);
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [editingFiles, setEditingFiles] = useState([]);
  const [uploading, setUploading] = useState(false);

  const load = () => fetchPackages().then((res) => setItems(res.data));
  useEffect(() => {
    load();
    fetchNodes().then((res) => setNodes(res.data));
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const nodeName = (id) => nodes.find((n) => n.id === Number(id))?.name || `#${id}`;

  const addConn = () => setForm((f) => ({ ...f, connections: [...f.connections, { ...emptyConn }] }));
  const removeConn = (idx) => setForm((f) => ({ ...f, connections: f.connections.filter((_, i) => i !== idx) }));
  const updateConn = (idx, k, v) =>
    setForm((f) => ({ ...f, connections: f.connections.map((c, i) => (i === idx ? { ...c, [k]: v } : c)) }));

  const openCreate = () => {
    setEditingId(null);
    setForm(emptyForm);
    setEditingFiles([]);
    setError("");
    setOpen(true);
  };

  const openEdit = (pkg) => {
    setEditingId(pkg.id);
    setForm({
      ...emptyForm,
      ...pkg,
      cooperation_price: pkg.cooperation_price ?? "",
      max_concurrent_sessions: pkg.max_concurrent_sessions ?? "",
      custom_message: pkg.custom_message || "",
      connections: (pkg.connections || []).map((c) => ({
        node_id: c.node_id,
        protocol: c.protocol,
        flow: c.flow || "",
      })),
    });
    setEditingFiles(pkg.files || []);
    setError("");
    setOpen(true);
  };

  const onUploadFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !editingId) return;
    setUploading(true);
    setError("");
    try {
      const res = await uploadPackageFile(editingId, file);
      setEditingFiles((files) => [...files, res.data]);
    } catch (err) {
      setError(err?.response?.data?.detail || "خطا در آپلود فایل");
    } finally {
      setUploading(false);
    }
  };

  const onDeleteFile = async (fileId) => {
    if (!editingId) return;
    await deletePackageFile(editingId, fileId);
    setEditingFiles((files) => files.filter((f) => f.id !== fileId));
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      const payload = {
        ...form,
        cooperation_price: form.cooperation_price === "" ? null : Number(form.cooperation_price),
        max_concurrent_sessions: form.max_concurrent_sessions === "" ? null : Number(form.max_concurrent_sessions),
        connections: form.connections.filter((c) => c.node_id),
      };
      if (editingId) {
        await updatePackage(editingId, payload);
      } else {
        await createPackage(payload);
      }
      setOpen(false);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || "خطا در ذخیره پکیج");
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (id) => {
    if (!confirm("این پکیج حذف شود؟ (روی کاربرهایی که قبلا با این پکیج خریده‌اند اثری ندارد)")) return;
    await deletePackage(id);
    load();
  };

  const onToggle = async (pkg) => {
    await updatePackage(pkg.id, { enabled: !pkg.enabled });
    load();
  };

  return (
    <Layout>
      <Topbar title="پکیج‌ها" subtitle="پلن‌های قابل خرید که ربات فروش به مشتری‌ها نشان می‌دهد" />

      <div className="flex justify-end mb-4">
        <button className="btn-primary" onClick={openCreate}>
          <Plus size={16} /> پکیج جدید
        </button>
      </div>

      <div className="card !p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 text-xs">
            <tr>
              <th className="text-right font-medium px-4 py-3">نام</th>
              <th className="text-right font-medium px-4 py-3">حجم</th>
              <th className="text-right font-medium px-4 py-3">مدت</th>
              <th className="text-right font-medium px-4 py-3">قیمت (تومان)</th>
              <th className="text-right font-medium px-4 py-3">وضعیت</th>
              <th className="text-right font-medium px-4 py-3">عملیات</th>
            </tr>
          </thead>
          <tbody>
            {items.map((p) => (
              <tr key={p.id} className="border-t border-gray-50 hover:bg-gray-50/60">
                <td className="px-4 py-3">
                  <div className="font-medium text-gray-800">{p.name}</div>
                  {p.description && <div className="text-xs text-gray-400">{p.description}</div>}
                  {p.connections?.length > 0 && (
                    <div className="text-xs text-brand-600 flex items-center gap-1 mt-1">
                      <Server size={12} /> {p.connections.length} سرویس همراه پکیج
                      {p.max_concurrent_sessions ? ` — حداکثر ${p.max_concurrent_sessions} اتصال همزمان` : ""}
                    </div>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-600">{p.quota_gb ? `${p.quota_gb} GB` : "نامحدود"}</td>
                <td className="px-4 py-3 text-gray-600">{p.duration_days ? `${p.duration_days} روز` : "بدون انقضا"}</td>
                <td className="px-4 py-3 text-gray-600" dir="ltr">
                  {formatToman(p.price)}
                  {p.cooperation_price != null && (
                    <div className="text-xs text-gray-400">همکاری: {formatToman(p.cooperation_price)}</div>
                  )}
                </td>
                <td className="px-4 py-3">
                  <span className={`badge ${p.enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                    {p.enabled ? "فعال" : "غیرفعال"}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <button title={p.enabled ? "غیرفعال کردن" : "فعال کردن"} onClick={() => onToggle(p)} className="text-gray-400 hover:text-brand-600">
                      <Power size={16} />
                    </button>
                    <button title="ویرایش" onClick={() => openEdit(p)} className="text-gray-400 hover:text-brand-600">
                      <Pencil size={16} />
                    </button>
                    <button title="حذف" onClick={() => onDelete(p.id)} className="text-gray-400 hover:text-red-600">
                      <Trash2 size={16} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={6} className="text-center text-gray-400 py-10">
                  <PackageIcon size={28} className="mx-auto mb-2 text-gray-300" />
                  هنوز پکیجی ساخته نشده است
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <Modal open={open} onClose={() => setOpen(false)} title={editingId ? "ویرایش پکیج" : "پکیج جدید"} width="max-w-2xl">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">نام پکیج *</label>
            <input className="input" required placeholder="مثلا: طلایی ۲۰ گیگ ماهانه" value={form.name} onChange={(e) => set("name", e.target.value)} />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">حجم (GB)</label>
              <input type="number" step="0.1" min="0" className="input" value={form.quota_gb} onChange={(e) => set("quota_gb", Number(e.target.value))} />
              <div className="text-xs text-gray-400 mt-1">۰ = نامحدود</div>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">مدت (روز)</label>
              <input type="number" min="0" className="input" value={form.duration_days ?? ""} onChange={(e) => set("duration_days", e.target.value ? Number(e.target.value) : null)} />
              <div className="text-xs text-gray-400 mt-1">خالی = بدون انقضا</div>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">قیمت (تومان)</label>
              <input type="number" min="0" className="input" value={form.price} onChange={(e) => set("price", Number(e.target.value))} />
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">قیمت همکاری / عمده‌فروشی (تومان)</label>
            <input
              type="number"
              min="0"
              className="input"
              placeholder="خالی = مثل قیمت عادی بالا"
              value={form.cooperation_price}
              onChange={(e) => set("cooperation_price", e.target.value)}
            />
            <div className="text-xs text-gray-400 mt-1">
              وقتی یک ادمین فرعی (نه ادمین اصلی) از این پکیج برای ساخت کاربر استفاده کند، این مبلغ (نه قیمت بالا) از اعتبار خودش کم می‌شود.
            </div>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">توضیحات (اختیاری، به مشتری نشان داده می‌شود)</label>
            <textarea className="input" rows={2} value={form.description || ""} onChange={(e) => set("description", e.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">ترتیب نمایش</label>
              <input type="number" className="input" value={form.sort_order} onChange={(e) => set("sort_order", Number(e.target.value))} />
            </div>
            <label className="flex items-center gap-2 mt-6 text-sm text-gray-600">
              <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
              فعال (در ربات نمایش داده شود)
            </label>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">حداکثر اتصال هم‌زمان (کل پکیج)</label>
            <input
              type="number"
              min="0"
              className="input"
              placeholder="خالی = نامحدود"
              value={form.max_concurrent_sessions}
              onChange={(e) => set("max_concurrent_sessions", e.target.value)}
            />
            <div className="text-xs text-gray-400 mt-1">
              این عدد روی مجموع همه‌ی سرویس‌های زیر اعمال می‌شود (نه هرکدام جدا) - مثلا پکیج با ۴ سرویس و عدد ۱ یعنی کاربر فقط
              روی یکی از آن‌ها همزمان می‌تواند وصل باشد، نه هر ۴ تا با هم.
            </div>
          </div>

          <div className="border-t border-gray-100 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-medium text-gray-700">
                سرویس‌های همراه پکیج (اختیاری، با انتخاب این پکیج خودکار برای کاربر ساخته می‌شود)
              </div>
              <button type="button" className="btn-secondary" onClick={addConn}>
                <Plus size={14} /> افزودن سرویس
              </button>
            </div>
            {form.connections.length === 0 && (
              <div className="text-xs text-gray-400">هیچ سرویسی اضافه نشده - این پکیج فقط حجم/مدت/قیمت تعریف می‌کند.</div>
            )}
            {form.connections.map((c, idx) => (
              <div key={idx} className="grid grid-cols-4 gap-2 mb-2 items-center">
                <select className="input col-span-2" value={c.node_id} onChange={(e) => updateConn(idx, "node_id", e.target.value)}>
                  <option value="">انتخاب سرور...</option>
                  {nodes.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.name}
                    </option>
                  ))}
                </select>
                <select className="input" value={c.protocol} onChange={(e) => updateConn(idx, "protocol", e.target.value)}>
                  {Object.entries(PROTOCOL_LABELS).map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </select>
                <div className="flex items-center gap-1">
                  {c.protocol === "xray" ? (
                    <input
                      className="input"
                      placeholder="flow (اختیاری)"
                      value={c.flow}
                      onChange={(e) => updateConn(idx, "flow", e.target.value)}
                    />
                  ) : (
                    <div className="flex-1" />
                  )}
                  <button type="button" className="text-gray-400 hover:text-red-600" onClick={() => removeConn(idx)}>
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div className="border-t border-gray-100 pt-3">
            <label className="block text-sm text-gray-600 mb-1">
              پیام دلخواه بعد از خرید (اختیاری، در ربات به مشتری فرستاده می‌شود)
            </label>
            <textarea
              className="input"
              rows={3}
              placeholder="مثلا: راهنمای نصب اپلیکیشن، شماره پشتیبانی و..."
              value={form.custom_message}
              onChange={(e) => set("custom_message", e.target.value)}
            />
          </div>

          <div className="border-t border-gray-100 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-medium text-gray-700">
                فایل‌های همراه پکیج (اختیاری، بعد از خرید در ربات فرستاده می‌شود)
              </div>
              {editingId ? (
                <label className="btn-secondary cursor-pointer">
                  <Paperclip size={14} /> {uploading ? "در حال آپلود..." : "افزودن فایل"}
                  <input type="file" className="hidden" onChange={onUploadFile} disabled={uploading} />
                </label>
              ) : null}
            </div>
            {!editingId && (
              <div className="text-xs text-gray-400">برای افزودن فایل، ابتدا پکیج را ذخیره کنید و دوباره ویرایش را باز کنید.</div>
            )}
            {editingId && editingFiles.length === 0 && (
              <div className="text-xs text-gray-400">هنوز فایلی اضافه نشده.</div>
            )}
            {editingFiles.map((f) => (
              <div key={f.id} className="flex items-center justify-between gap-2 py-1.5 px-2 rounded-lg bg-gray-50 mb-1.5 text-sm">
                <div className="flex items-center gap-2 text-gray-700 truncate">
                  <Download size={14} className="text-gray-400 shrink-0" />
                  <span className="truncate">{f.filename}</span>
                  <span className="text-xs text-gray-400 shrink-0">({formatFileSize(f.size_bytes)})</span>
                </div>
                <button type="button" className="text-gray-400 hover:text-red-600 shrink-0" onClick={() => onDeleteFile(f.id)}>
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>

          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              انصراف
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? "در حال ذخیره..." : "ذخیره پکیج"}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
