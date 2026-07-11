import React, { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, ShieldCheck, Users as UsersIcon, Link2, Wallet, Send, Wand2, Eye, EyeOff, UsersRound } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import {
  fetchAdmins,
  fetchPermissionChoices,
  createAdmin,
  updateAdmin,
  deleteAdmin,
  fetchAdminGroups,
  createAdminGroup,
  updateAdminGroup,
  deleteAdminGroup,
} from "../api/client.js";

function formatToman(n) {
  return new Intl.NumberFormat("fa-IR").format(n || 0);
}

// یوزرنیم/پسورد رندوم برای دکمه‌های "تولید خودکار" - فقط حروف/عدد لاتین تا
// همه‌جا (لاگین پنل، URL اختصاصی و غیره) بدون مشکل کاراکترهای فارسی کار کنه.
function randomToken(length, alphabet) {
  let out = "";
  for (let i = 0; i < length; i++) out += alphabet[Math.floor(Math.random() * alphabet.length)];
  return out;
}
function generateUsername() {
  return "admin" + randomToken(5, "abcdefghijkmnpqrstuvwxyz23456789");
}
function generatePassword() {
  const upper = "ABCDEFGHJKLMNPQRSTUVWXYZ";
  const lower = "abcdefghijkmnpqrstuvwxyz";
  const digits = "23456789";
  const symbols = "!@#$%";
  const all = upper + lower + digits + symbols;
  // حداقل یکی از هر دسته، بقیه رندوم - تا همیشه یه پسورد "قوی" تولید بشه
  const required = [randomToken(1, upper), randomToken(1, lower), randomToken(1, digits), randomToken(1, symbols)];
  const rest = randomToken(8, all).split("");
  return [...required, ...rest].sort(() => Math.random() - 0.5).join("");
}

const emptyForm = { username: "", password: "", permissions: [], login_slug: "", balance: "", telegram_id: "", group_id: "" };
const emptyGroupForm = { name: "", permissions: [] };

export default function Admins() {
  const [items, setItems] = useState([]);
  const [choices, setChoices] = useState({});
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  const [groups, setGroups] = useState([]);
  const [groupOpen, setGroupOpen] = useState(false);
  const [editingGroupId, setEditingGroupId] = useState(null);
  const [groupForm, setGroupForm] = useState(emptyGroupForm);
  const [groupError, setGroupError] = useState("");
  const [groupSaving, setGroupSaving] = useState(false);

  const load = () => fetchAdmins().then((res) => setItems(res.data));
  const loadGroups = () => fetchAdminGroups().then((res) => setGroups(res.data));
  useEffect(() => {
    load();
    loadGroups();
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
    setShowPassword(false);
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
      group_id: admin.group_id || "",
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
          group_id: form.group_id === "" ? 0 : Number(form.group_id),
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
          group_id: form.group_id === "" ? null : Number(form.group_id),
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

  // ---------- Permission groups ----------
  const toggleGroupPerm = (key) =>
    setGroupForm((f) => ({
      ...f,
      permissions: f.permissions.includes(key) ? f.permissions.filter((p) => p !== key) : [...f.permissions, key],
    }));

  const openGroupCreate = () => {
    setEditingGroupId(null);
    setGroupForm(emptyGroupForm);
    setGroupError("");
    setGroupOpen(true);
  };

  const openGroupEdit = (g) => {
    setEditingGroupId(g.id);
    setGroupForm({ name: g.name, permissions: g.permissions || [] });
    setGroupError("");
    setGroupOpen(true);
  };

  const submitGroup = async (e) => {
    e.preventDefault();
    setGroupSaving(true);
    setGroupError("");
    try {
      if (editingGroupId) {
        await updateAdminGroup(editingGroupId, groupForm);
      } else {
        await createAdminGroup(groupForm);
      }
      setGroupOpen(false);
      loadGroups();
      load();
    } catch (err) {
      setGroupError(err?.response?.data?.detail || "خطا در ذخیره گروه");
    } finally {
      setGroupSaving(false);
    }
  };

  const onDeleteGroup = async (g) => {
    if (!confirm(`گروه «${g.name}» حذف شود؟ ادمین‌های این گروه حذف نمی‌شوند، فقط بدون گروه می‌مانند.`)) return;
    await deleteAdminGroup(g.id);
    loadGroups();
    load();
  };

  return (
    <Layout>
      <Topbar title="مدیریت ادمین‌ها" subtitle="ادمین‌های فرعی با دسترسی محدود و گروه کاربران مخصوص خودشان" />

      <div className="card mb-6">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2 font-medium text-gray-700">
            <UsersRound size={16} className="text-violet-500" /> گروه‌های دسترسی
          </div>
          <button type="button" className="btn-secondary" onClick={openGroupCreate}>
            <Plus size={14} /> گروه جدید
          </button>
        </div>
        {groups.length === 0 ? (
          <div className="text-sm text-gray-400">
            هنوز گروهی نساخته‌اید. با گروه می‌توانید یک بار دسترسی‌ها را تعریف کنید و به چند ادمین بدهید - با ویرایش گروه، دسترسی همه آن‌ها یک‌جا تغییر می‌کند.
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            {groups.map((g) => (
              <div key={g.id} className="flex items-center gap-2 bg-gray-50 rounded-xl px-3 py-2 text-sm">
                <span className="font-medium text-gray-700">{g.name}</span>
                <span className="text-xs text-gray-400">
                  ({g.permissions?.length ? g.permissions.map((p) => choices[p] || p).join("، ") : "فقط کاربران"})
                </span>
                <span className="text-xs text-gray-400">· {g.admins_count} ادمین</span>
                <button title="ویرایش گروه" onClick={() => openGroupEdit(g)} className="text-gray-400 hover:text-brand-600">
                  <Pencil size={14} />
                </button>
                <button title="حذف گروه" onClick={() => onDeleteGroup(g)} className="text-gray-400 hover:text-red-600">
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

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
                  {a.is_superadmin ? (
                    "همه چیز"
                  ) : a.group_name ? (
                    <span className="badge bg-violet-50 text-violet-600 flex items-center gap-1 w-fit">
                      <UsersRound size={12} /> گروه: {a.group_name}
                    </span>
                  ) : a.permissions?.length ? (
                    a.permissions.map((p) => choices[p] || p).join("، ")
                  ) : (
                    "فقط کاربران"
                  )}
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
              <div className="flex gap-2">
                <input className="input flex-1" required dir="ltr" value={form.username} onChange={(e) => set("username", e.target.value)} />
                <button
                  type="button"
                  className="btn-secondary shrink-0"
                  title="تولید خودکار یوزرنیم"
                  onClick={() => set("username", generateUsername())}
                >
                  <Wand2 size={16} />
                </button>
              </div>
            </div>
          )}
          <div>
            <label className="block text-sm text-gray-600 mb-1">
              {editingId ? "رمز عبور جدید (اختیاری)" : "رمز عبور *"}
            </label>
            <div className="flex gap-2">
              <input
                type={showPassword ? "text" : "password"}
                className="input flex-1"
                dir="ltr"
                required={!editingId}
                placeholder={editingId ? "خالی = بدون تغییر" : ""}
                value={form.password}
                onChange={(e) => set("password", e.target.value)}
              />
              <button
                type="button"
                className="btn-secondary shrink-0"
                title={showPassword ? "پنهان کردن رمز" : "نمایش رمز"}
                onClick={() => setShowPassword((s) => !s)}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
              <button
                type="button"
                className="btn-secondary shrink-0"
                title="تولید خودکار رمز عبور قوی"
                onClick={() => {
                  set("password", generatePassword());
                  setShowPassword(true);
                }}
              >
                <Wand2 size={16} />
              </button>
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">گروه دسترسی (اختیاری)</label>
            <select className="input" value={form.group_id} onChange={(e) => set("group_id", e.target.value)}>
              <option value="">بدون گروه (دسترسی دستی از چک‌باکس‌های پایین)</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
            <div className="text-xs text-gray-400 mt-1">
              اگه گروه انتخاب بشه، دسترسی این ادمین از خود گروه می‌آید و چک‌باکس‌های پایین نادیده گرفته می‌شوند.
            </div>
          </div>

          <div className={form.group_id ? "opacity-40 pointer-events-none" : ""}>
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

      <Modal open={groupOpen} onClose={() => setGroupOpen(false)} title={editingGroupId ? "ویرایش گروه" : "گروه جدید"} width="max-w-md">
        <form onSubmit={submitGroup} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">نام گروه *</label>
            <input
              className="input"
              required
              value={groupForm.name}
              onChange={(e) => setGroupForm((f) => ({ ...f, name: e.target.value }))}
              placeholder="مثلا: پشتیبان، فروش"
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-2">دسترسی‌ها (مدیریت کاربران همیشه فعال است)</label>
            <div className="space-y-1.5">
              {Object.entries(choices).map(([key, label]) => (
                <label key={key} className="flex items-center gap-2 text-sm text-gray-600">
                  <input type="checkbox" checked={groupForm.permissions.includes(key)} onChange={() => toggleGroupPerm(key)} />
                  {label}
                </label>
              ))}
            </div>
          </div>
          {groupError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{groupError}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setGroupOpen(false)}>
              انصراف
            </button>
            <button type="submit" disabled={groupSaving} className="btn-primary">
              {groupSaving ? "در حال ذخیره..." : "ذخیره گروه"}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
