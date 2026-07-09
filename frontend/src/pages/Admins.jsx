import React, { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, ShieldCheck, Users as UsersIcon, Link2, Wallet, Send } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import { fetchAdmins, fetchPermissionChoices, createAdmin, updateAdmin, deleteAdmin } from "../api/client.js";

function formatToman(n) {
  return new Intl.NumberFormat("fa-IR").format(n || 0);
}

const emptyForm = { username: "", password: "", permissions: [], login_slug: "", balance: "", telegram_id: "" };

export default function Admins() {
  const [items, setItems] = useState([]);
  const [choices, setChoices] = useState({});
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const load = () => fetchAdmins().then((res) => setItems(res.data));
  useEffect(() => {
    load();
    fetchPermissionChoices().then((res) => setChoices(res.data));
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const togglePerm = (key) =>
    setForm((f) => ({
      ...f,
      permissions: f.permissions.includes(key) ? f.permissions.filter((p) => p !== key) : [...f.permissions, key],
    }));

  const openCreate = () => {
    setEditingId(null);
    setForm(emptyForm);
    setError("");
    setOpen(true);
  };

  const openEdit = (admin) => {
    setEditingId(admin.id);
    setForm({
      username: admin.username,
      password: "",
      permissions: admin.permissions || [],
      login_slug: admin.login_slug || "",
      balance: admin.balance || 0,
      telegram_id: admin.telegram_id || "",
    });
    setError("");
    setOpen(true);
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      if (editingId) {
        const payload = {
          permissions: form.permissions,
          login_slug: form.login_slug || null,
          balance: form.balance === "" ? null : Number(form.balance),
          telegram_id: form.telegram_id === "" ? null : Number(form.telegram_id),
        };
        if (form.password) payload.password = form.password;
        await updateAdmin(editingId, payload);
      } else {
        await createAdmin({
          username: form.username,
          password: form.password,
          permissions: form.permissions,
          login_slug: form.login_slug || null,
          telegram_id: form.telegram_id === "" ? null : Number(form.telegram_id),
        });
      }
      setOpen(false);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || "خطا در ذخیره ادمین");
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (admin) => {
    if (!confirm(`ادمین «${admin.username}» حذف شود؟ کاربرهای این ادمین حذف نمی‌شوند و فقط بدون گروه می‌مانند.`)) return;
    await deleteAdmin(admin.id);
    load();
  };

  return (
    <Layout>
      <Topbar title="مدیریت ادمین‌ها" subtitle="ادمین‌های فرعی با دسترسی محدود و گروه کاربران مخصوص خودشان" />

      <div className="flex justify-end mb-4">
        <button className="btn-primary" onClick={openCreate}>
          <Plus size={16} /> ادمین جدید
        </button>
      </div>

      <div className="card !p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 text-xs">
            <tr>
              <th className="text-right font-medium px-4 py-3">نام کاربری</th>
              <th className="text-right font-medium px-4 py-3">نقش</th>
              <th className="text-right font-medium px-4 py-3">دسترسی‌ها</th>
              <th className="text-right font-medium px-4 py-3">تعداد کاربران</th>
              <th className="text-right font-medium px-4 py-3">اعتبار</th>
              <th className="text-right font-medium px-4 py-3">ربات تلگرام</th>
              <th className="text-right font-medium px-4 py-3">لینک ورود</th>
              <th className="text-right font-medium px-4 py-3">عملیات</th>
            </tr>
          </thead>
          <tbody>
            {items.map((a) => (
              <tr key={a.id} className="border-t border-gray-50 hover:bg-gray-50/60">
                <td className="px-4 py-3 font-medium text-gray-800">{a.username}</td>
                <td className="px-4 py-3">
                  {a.is_superadmin ? (
                    <span className="badge bg-brand-50 text-brand-600 flex items-center gap-1 w-fit">
                      <ShieldCheck size={12} /> ادمین اصلی
                    </span>
                  ) : (
                    <span className="badge bg-gray-100 text-gray-500 w-fit">ادمین فرعی</span>
                  )}
                </td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  {a.is_superadmin ? "همه چیز" : a.permissions?.length ? a.permissions.map((p) => choices[p] || p).join("، ") : "فقط کاربران"}
                </td>
                <td className="px-4 py-3 text-gray-600">
                  <span className="flex items-center gap-1">
                    <UsersIcon size={13} className="text-gray-400" /> {a.users_count}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-600" dir="ltr">
                  {!a.is_superadmin && (
                    <span className="flex items-center gap-1 justify-end">
                      <Wallet size={12} className="text-gray-400" /> {formatToman(a.balance)}
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-500 text-xs" dir="ltr">
                  {a.telegram_id ? (
                    <span className="flex items-center gap-1 justify-end">
                      <Send size={12} /> {a.telegram_id}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-4 py-3 text-gray-500 text-xs" dir="ltr">
                  {a.login_slug ? (
                    <span className="flex items-center gap-1 justify-end">
                      <Link2 size={12} /> /a/{a.login_slug}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-4 py-3">
                  {!a.is_superadmin && (
                    <div className="flex items-center gap-2">
                      <button title="ویرایش" onClick={() => openEdit(a)} className="text-gray-400 hover:text-brand-600">
                        <Pencil size={16} />
                      </button>
                      <button title="حذف" onClick={() => onDelete(a)} className="text-gray-400 hover:text-red-600">
                        <Trash2 size={16} />
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={8} className="text-center text-gray-400 py-10">
                  هنوز ادمینی ساخته نشده است
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <Modal open={open} onClose={() => setOpen(false)} title={editingId ? "ویرایش ادمین" : "ادمین جدید"} width="max-w-lg">
        <form onSubmit={submit} className="space-y-4">
          {!editingId && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">نام کاربری *</label>
              <input className="input" required value={form.username} onChange={(e) => set("username", e.target.value)} />
            </div>
          )}
          <div>
            <label className="block text-sm text-gray-600 mb-1">
              {editingId ? "رمز عبور جدید (اختیاری)" : "رمز عبور *"}
            </label>
            <input
              type="password"
              className="input"
              required={!editingId}
              placeholder={editingId ? "خالی = بدون تغییر" : ""}
              value={form.password}
              onChange={(e) => set("password", e.target.value)}
            />
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-2">دسترسی‌ها (مدیریت کاربران همیشه فعال است)</label>
            <div className="space-y-1.5">
              {Object.entries(choices).map(([key, label]) => (
                <label key={key} className="flex items-center gap-2 text-sm text-gray-600">
                  <input type="checkbox" checked={form.permissions.includes(key)} onChange={() => togglePerm(key)} />
                  {label}
                </label>
              ))}
            </div>
          </div>

          {editingId && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">موجودی اعتبار (تومان)</label>
              <input
                type="number"
                className="input"
                value={form.balance}
                onChange={(e) => set("balance", e.target.value)}
              />
              <div className="text-xs text-gray-400 mt-1">
                این ادمین هنگام ساخت کاربر با پکیج، قیمت همکاری آن پکیج را از همین اعتبار پرداخت می‌کند.
              </div>
            </div>
          )}

          <div>
            <label className="block text-sm text-gray-600 mb-1">آیدی عددی تلگرام (اختیاری)</label>
            <input
              type="number"
              className="input"
              dir="ltr"
              placeholder="خالی = دسترسی به ربات ندارد"
              value={form.telegram_id}
              onChange={(e) => set("telegram_id", e.target.value)}
            />
            <div className="text-xs text-gray-400 mt-1">
              اگه پر بشه، این ادمین می‌تونه با همون آیدی تلگرامش وارد ربات بشه و فقط کاربران خودش رو مدیریت کنه (بدون دیدن کاربران بقیه ادمین‌ها).
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">لینک ورود اختصاصی (اختیاری)</label>
            <div className="flex items-center gap-2" dir="ltr">
              <span className="text-gray-400 text-sm">/a/</span>
              <input
                className="input"
                placeholder="مثلا: ali"
                value={form.login_slug}
                onChange={(e) => set("login_slug", e.target.value.replace(/[^a-zA-Z0-9_-]/g, ""))}
              />
            </div>
            <div className="text-xs text-gray-400 mt-1">
              فقط یک لینک ورود جداگانه است (بدون دامنه یا DNS واقعی) - همان صفحه ورود پنل را باز می‌کند.
            </div>
          </div>

          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              انصراف
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? "در حال ذخیره..." : "ذخیره ادمین"}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
