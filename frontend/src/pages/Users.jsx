import React, { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Plus, Search, Trash2, RotateCcw, Network, Layers, PencilLine, ChevronRight, ChevronLeft, X, ArrowUpDown, FileDown } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import QuotaBar from "../components/QuotaBar.jsx";
import {
  fetchUsers,
  createUser,
  deleteUser,
  resetUsage,
  bulkCreateUsers,
  bulkUpdateUsers,
  bulkDeleteUsers,
  fetchNodes,
  fetchPackages,
  fetchAdmins,
  exportUsers,
} from "../api/client.js";
import { STATUS_LABELS, STATUS_STYLES, formatDate, gbToBytes, downloadBlob } from "../utils.js";
import { useAuth } from "../context/AuthContext.jsx";

const PAGE_SIZE = 50;

const emptyBulkConn = { node_id: "", protocol: "openvpn", max_concurrent_sessions: 1 };

const STATUS_FILTER_OPTIONS = [
  { value: "", label: "همه وضعیت‌ها" },
  { value: "active", label: "فعال" },
  { value: "disabled", label: "غیرفعال" },
  { value: "quota_exceeded", label: "اتمام حجم" },
  { value: "expired", label: "منقضی‌شده" },
];

const SORT_OPTIONS = [
  { value: "id", label: "جدیدترین" },
  { value: "username", label: "نام کاربری" },
  { value: "used_bytes", label: "مصرف" },
  { value: "total_quota_bytes", label: "حجم مجاز" },
  { value: "expire_at", label: "تاریخ انقضا" },
  { value: "status", label: "وضعیت" },
];

export default function Users() {
  const { isSuperadmin } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const [users, setUsers] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [statusFilter, setStatusFilter] = useState(searchParams.get("status") || "");
  const [onlineOnly, setOnlineOnly] = useState(searchParams.get("online_only") === "1");
  const [sortBy, setSortBy] = useState("id");
  const [sortDir, setSortDir] = useState("desc");
  const [selected, setSelected] = useState(new Set());
  const [nodes, setNodes] = useState([]);
  const [admins, setAdmins] = useState([]);
  const [ownerAdminFilter, setOwnerAdminFilter] = useState("");

  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();

  const emptyCreateForm = {
    username: "",
    full_name: "",
    package_id: "",
    quota_gb: "",
    max_concurrent_sessions: "",
    expire_mode: "none", // none | date | days_from_now | first_use
    expire_at: "",
    expire_days: "",
    notes: "",
    owner_admin_id: "",
  };
  const [packages, setPackages] = useState([]);
  const [form, setForm] = useState(emptyCreateForm);

  const [bulkCreateOpen, setBulkCreateOpen] = useState(false);
  const [bulkCreateForm, setBulkCreateForm] = useState({
    prefix: "user",
    count: 5,
    package_id: "",
    quota_gb: "",
    expire_days: "",
    notes: "",
    connections: [],
  });
  const [bulkCreateResult, setBulkCreateResult] = useState(null);
  const [bulkCreateError, setBulkCreateError] = useState("");

  const [bulkEditOpen, setBulkEditOpen] = useState(false);
  const [bulkEditForm, setBulkEditForm] = useState({
    add_gb: "",
    add_days: "",
    reset_usage: false,
    status: "",
    max_concurrent_sessions: "",
  });
  const [bulkEditError, setBulkEditError] = useState("");
  const [exporting, setExporting] = useState(false);

  const totalPages = Math.max(Math.ceil(total / PAGE_SIZE), 1);

  const onExport = async () => {
    setExporting(true);
    try {
      const res = await exportUsers(search, {
        status: statusFilter,
        onlineOnly,
        ownerAdminId: ownerAdminFilter,
      });
      const filename =
        (res.headers["content-disposition"] || "").match(/filename="?([^"]+)"?/)?.[1] || "users_export.xlsx";
      downloadBlob(filename, res.data);
    } catch (err) {
      alert("خطا در اکسپورت خروجی اکسل");
    } finally {
      setExporting(false);
    }
  };

  const load = () =>
    fetchUsers(page, PAGE_SIZE, search, {
      status: statusFilter,
      onlineOnly,
      sortBy,
      sortDir,
      ownerAdminId: ownerAdminFilter,
    }).then((res) => {
      setUsers(res.data.items);
      setTotal(res.data.total);
    });

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, search, statusFilter, onlineOnly, sortBy, sortDir, ownerAdminFilter]);

  useEffect(() => {
    fetchNodes().then((res) => setNodes(res.data));
    fetchPackages().then((res) => setPackages(res.data.filter((p) => p.enabled)));
    if (isSuperadmin) fetchAdmins().then((res) => setAdmins(res.data));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Non-superadmin admins must always build a user from a package (see
  // "بدون پکیج" being hidden from them below - manual/no-package creation
  // would bypass the cooperation-price wallet charge entirely). Once
  // packages load, default both create forms to the first one so the
  // (now required, no-empty-option) select always has something valid
  // selected instead of showing blank.
  useEffect(() => {
    if (isSuperadmin || packages.length === 0) return;
    setForm((f) => (f.package_id ? f : { ...f, package_id: packages[0].id }));
    setBulkCreateForm((f) => (f.package_id ? f : { ...f, package_id: packages[0].id }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSuperadmin, packages]);

  // debounce search input -> server-side search param
  useEffect(() => {
    const t = setTimeout(() => {
      setPage(1);
      setSearch(searchInput.trim());
    }, 350);
    return () => clearTimeout(t);
  }, [searchInput]);

  // keep the URL in sync so the dashboard's "کاربران آنلاین الان"/status links
  // are shareable/bookmarkable and survive a page refresh
  useEffect(() => {
    const params = {};
    if (statusFilter) params.status = statusFilter;
    if (onlineOnly) params.online_only = "1";
    setSearchParams(params, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, onlineOnly]);

  const toggleSortDir = () => setSortDir((d) => (d === "asc" ? "desc" : "asc"));

  const clearFilters = () => {
    setStatusFilter("");
    setOnlineOnly(false);
    setPage(1);
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      const payload = {
        username: form.username,
        full_name: form.full_name || null,
        notes: form.notes || null,
      };
      if (isSuperadmin && form.owner_admin_id) {
        payload.owner_admin_id = Number(form.owner_admin_id);
      }
      if (form.package_id) {
        // quota/expiry/connections are all derived server-side from the package
        payload.package_id = Number(form.package_id);
      } else {
        payload.total_quota_bytes = form.quota_gb ? gbToBytes(form.quota_gb) : 0;
        if (form.expire_mode === "date") {
          payload.expire_at = form.expire_at ? new Date(form.expire_at).toISOString() : null;
        } else if (form.expire_mode === "days_from_now") {
          const days = Number(form.expire_days) || 0;
          payload.expire_at = days ? new Date(Date.now() + days * 86400000).toISOString() : null;
        } else if (form.expire_mode === "first_use") {
          payload.expire_days_after_first_use = Number(form.expire_days) || null;
        }
      }
      const res = await createUser(payload);
      setOpen(false);
      const defaultMaxSessions = form.max_concurrent_sessions !== "" ? Number(form.max_concurrent_sessions) : undefined;
      setForm(emptyCreateForm);
      navigate(`/users/${res.data.id}`, { state: { defaultMaxSessions } });
    } catch (err) {
      setError(err?.response?.data?.detail || "خطا در ساخت کاربر");
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (id) => {
    if (!confirm("این کاربر و تمام کانکشن‌هایش حذف شود؟")) return;
    await deleteUser(id);
    load();
  };

  const onReset = async (id) => {
    await resetUsage(id);
    load();
  };

  // ---------------- selection ----------------
  const toggleOne = (id) => {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allOnPageSelected = users.length > 0 && users.every((u) => selected.has(u.id));

  const toggleAllOnPage = () => {
    setSelected((s) => {
      const next = new Set(s);
      if (allOnPageSelected) {
        users.forEach((u) => next.delete(u.id));
      } else {
        users.forEach((u) => next.add(u.id));
      }
      return next;
    });
  };

  const clearSelection = () => setSelected(new Set());

  const onBulkDelete = async () => {
    if (selected.size === 0) return;
    if (!confirm(`${selected.size} کاربر انتخاب‌شده (و تمام اتصالاتشون) حذف بشن؟ این کار قابل بازگشت نیست.`)) return;
    await bulkDeleteUsers(Array.from(selected));
    clearSelection();
    load();
  };

  // ---------------- bulk create ----------------
  const openBulkCreate = () => {
    setBulkCreateForm({ prefix: "user", count: 5, package_id: "", quota_gb: "", expire_days: "", notes: "", connections: [] });
    setBulkCreateResult(null);
    setBulkCreateError("");
    setBulkCreateOpen(true);
  };

  const addBulkConn = () => {
    setBulkCreateForm((f) => ({ ...f, connections: [...f.connections, { ...emptyBulkConn }] }));
  };

  const updateBulkConn = (idx, key, value) => {
    setBulkCreateForm((f) => ({
      ...f,
      connections: f.connections.map((c, i) => (i === idx ? { ...c, [key]: value } : c)),
    }));
  };

  const removeBulkConn = (idx) => {
    setBulkCreateForm((f) => ({ ...f, connections: f.connections.filter((_, i) => i !== idx) }));
  };

  const submitBulkCreate = async (e) => {
    e.preventDefault();
    setSaving(true);
    setBulkCreateError("");
    setBulkCreateResult(null);
    try {
      const payload = {
        prefix: bulkCreateForm.prefix,
        count: Number(bulkCreateForm.count),
        notes: bulkCreateForm.notes || null,
      };
      if (bulkCreateForm.package_id) {
        // quota/expiry/connections all come from the package server-side
        payload.package_id = Number(bulkCreateForm.package_id);
      } else {
        payload.quota_gb = bulkCreateForm.quota_gb ? Number(bulkCreateForm.quota_gb) : 0;
        payload.expire_days = bulkCreateForm.expire_days ? Number(bulkCreateForm.expire_days) : null;
        payload.connections = bulkCreateForm.connections
          .filter((c) => c.node_id)
          .map((c) => ({
            node_id: Number(c.node_id),
            protocol: c.protocol,
            max_concurrent_sessions: Number(c.max_concurrent_sessions) || 0,
          }));
      }
      const res = await bulkCreateUsers(payload);
      setBulkCreateResult(res.data);
      load();
    } catch (err) {
      setBulkCreateError(err?.response?.data?.detail || "خطا در ساخت گروهی کاربران");
    } finally {
      setSaving(false);
    }
  };

  // ---------------- bulk edit ----------------
  const openBulkEdit = () => {
    setBulkEditForm({ add_gb: "", add_days: "", reset_usage: false, status: "", max_concurrent_sessions: "" });
    setBulkEditError("");
    setBulkEditOpen(true);
  };

  const submitBulkEdit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setBulkEditError("");
    try {
      const payload = {
        user_ids: Array.from(selected),
        add_gb: bulkEditForm.add_gb ? Number(bulkEditForm.add_gb) : 0,
        add_days: bulkEditForm.add_days ? Number(bulkEditForm.add_days) : 0,
        reset_usage: bulkEditForm.reset_usage,
        status: bulkEditForm.status || null,
        max_concurrent_sessions:
          bulkEditForm.max_concurrent_sessions !== "" ? Number(bulkEditForm.max_concurrent_sessions) : null,
      };
      await bulkUpdateUsers(payload);
      setBulkEditOpen(false);
      clearSelection();
      load();
    } catch (err) {
      setBulkEditError(err?.response?.data?.detail || "خطا در ویرایش گروهی");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Layout>
      <Topbar title="کاربران" subtitle={`${total} کاربر ثبت‌شده`} />

      <div className="card !p-4 mb-4">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-2.5 flex-wrap">
            <div className="relative">
              <Search className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" size={16} />
              <input
                className="input pr-9 w-56"
                placeholder="جستجوی کاربر..."
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
              />
            </div>

            <div className="w-px h-6 bg-gray-100 hidden sm:block" />

            <div className="flex items-center gap-2 flex-wrap">
              <select
                className="input w-auto min-w-[8rem] cursor-pointer"
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value);
                  setPage(1);
                }}
              >
                {STATUS_FILTER_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>

              <div className="flex items-center gap-1">
                <select
                  className="input w-auto min-w-[7.5rem] cursor-pointer"
                  value={sortBy}
                  onChange={(e) => {
                    setSortBy(e.target.value);
                    setPage(1);
                  }}
                  title="مرتب‌سازی بر اساس"
                >
                  {SORT_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn-secondary !px-2.5"
                  title={sortDir === "asc" ? "صعودی" : "نزولی"}
                  onClick={() => {
                    toggleSortDir();
                    setPage(1);
                  }}
                >
                  <ArrowUpDown size={16} />
                </button>
              </div>

              {isSuperadmin && (
                <select
                  className="input w-auto min-w-[8rem] cursor-pointer"
                  value={ownerAdminFilter}
                  onChange={(e) => {
                    setOwnerAdminFilter(e.target.value);
                    setPage(1);
                  }}
                  title="فیلتر بر اساس ادمین"
                >
                  <option value="">همه ادمین‌ها</option>
                  {admins.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.username}
                    </option>
                  ))}
                </select>
              )}
            </div>

            {onlineOnly && (
              <span className="badge bg-emerald-50 text-emerald-600 inline-flex items-center gap-1">
                فقط آنلاین‌ها
                <button type="button" className="hover:text-emerald-800" onClick={clearFilters}>
                  <X size={12} />
                </button>
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            {selected.size > 0 && (
              <div className="flex items-center gap-2 pl-2.5 ml-0.5 border-l border-gray-100">
                <span className="text-xs font-medium text-brand-700 bg-brand-50 rounded-full px-2.5 py-1">
                  {selected.size} انتخاب شده
                </span>
                <button className="btn-secondary" onClick={openBulkEdit}>
                  <PencilLine size={16} /> ویرایش گروهی
                </button>
                <button className="btn-danger" onClick={onBulkDelete}>
                  <Trash2 size={16} /> حذف گروهی
                </button>
                <button className="btn-secondary" onClick={clearSelection}>
                  لغو انتخاب
                </button>
              </div>
            )}
            <button className="btn-secondary" onClick={onExport} disabled={exporting}>
              <FileDown size={16} /> {exporting ? "..." : "اکسپورت اکسل"}
            </button>
            <button className="btn-secondary" onClick={openBulkCreate}>
              <Layers size={16} /> ساخت گروهی
            </button>
            <button className="btn-primary" onClick={() => setOpen(true)}>
              <Plus size={16} /> کاربر جدید
            </button>
          </div>
        </div>
      </div>

      <div className="card !p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 text-xs">
            <tr>
              <th className="text-right font-medium px-4 py-3 w-8">
                <input type="checkbox" checked={allOnPageSelected} onChange={toggleAllOnPage} />
              </th>
              <th className="text-right font-medium px-4 py-3">کاربر</th>
              {isSuperadmin && <th className="text-right font-medium px-4 py-3">ادمین</th>}
              <th className="text-right font-medium px-4 py-3">وضعیت</th>
              <th className="text-right font-medium px-4 py-3 w-56">مصرف</th>
              <th className="text-right font-medium px-4 py-3">اتصالات</th>
              <th className="text-right font-medium px-4 py-3">انقضا</th>
              <th className="text-right font-medium px-4 py-3">عملیات</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="border-t border-gray-50 hover:bg-gray-50/60">
                <td className="px-4 py-3">
                  <input type="checkbox" checked={selected.has(u.id)} onChange={() => toggleOne(u.id)} />
                </td>
                <td className="px-4 py-3">
                  <Link to={`/users/${u.id}`} className="font-medium text-gray-800 hover:text-brand-600 inline-flex items-center gap-1.5">
                    <span
                      className={`inline-block w-2 h-2 rounded-full ${u.online ? "bg-emerald-500" : "bg-gray-300"}`}
                      title={u.online ? "آنلاین" : "آفلاین"}
                    />
                    {u.username}
                  </Link>
                  {u.full_name && <div className="text-xs text-gray-400">{u.full_name}</div>}
                </td>
                {isSuperadmin && (
                  <td className="px-4 py-3 text-xs text-gray-500">
                    {u.owner_admin_username || <span className="text-gray-300">—</span>}
                  </td>
                )}
                <td className="px-4 py-3">
                  <span className={`badge ${STATUS_STYLES[u.status]}`}>{STATUS_LABELS[u.status]}</span>
                </td>
                <td className="px-4 py-3">
                  <QuotaBar used={u.used_bytes} total={u.total_quota_bytes} />
                </td>
                <td className="px-4 py-3 text-gray-500">
                  <span className="inline-flex items-center gap-1">
                    <Network size={14} /> {u.connections_count}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-500">
                  {!u.expire_at && u.expire_days_after_first_use ? (
                    <span className="text-amber-600" title="هنوز به این سرویس وصل نشده">
                      از اولین اتصال ({u.expire_days_after_first_use} روز)
                    </span>
                  ) : (
                    formatDate(u.expire_at)
                  )}
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <button title="ریست حجم مصرفی" onClick={() => onReset(u.id)} className="text-gray-400 hover:text-brand-600">
                      <RotateCcw size={16} />
                    </button>
                    <button title="حذف" onClick={() => onDelete(u.id)} className="text-gray-400 hover:text-red-600">
                      <Trash2 size={16} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={isSuperadmin ? 8 : 7} className="text-center text-gray-400 py-10">
                  کاربری یافت نشد
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="flex items-center justify-between px-4 py-3 border-t border-gray-50 text-sm text-gray-500">
          <div>
            صفحه {page} از {totalPages}
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-secondary" disabled={page <= 1} onClick={() => setPage((p) => Math.max(p - 1, 1))}>
              <ChevronRight size={14} /> قبلی
            </button>
            <button
              className="btn-secondary"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => Math.min(p + 1, totalPages))}
            >
              بعدی <ChevronLeft size={14} />
            </button>
          </div>
        </div>
      </div>

      {/* Single create modal */}
      <Modal open={open} onClose={() => setOpen(false)} title="افزودن کاربر جدید">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">نام کاربری *</label>
            <input className="input" required value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">نام کامل</label>
            <input className="input" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
          </div>

          {isSuperadmin && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">متعلق به ادمین</label>
              <select
                className="input"
                value={form.owner_admin_id}
                onChange={(e) => setForm({ ...form, owner_admin_id: e.target.value })}
              >
                <option value="">خودم (ادمین اصلی)</option>
                {admins.filter((a) => !a.is_superadmin).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.username}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div>
            <label className="block text-sm text-gray-600 mb-1">
              ساخت با پکیج{isSuperadmin ? " (اختیاری)" : ""}
            </label>
            <select
              className="input"
              required={!isSuperadmin}
              value={form.package_id}
              onChange={(e) => setForm({ ...form, package_id: e.target.value })}
            >
              {isSuperadmin && <option value="">بدون پکیج (تنظیم دستی)</option>}
              {packages.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} — {p.quota_gb ? `${p.quota_gb}GB` : "نامحدود"} / {p.duration_days ? `${p.duration_days} روز` : "بدون انقضا"}
                  {p.connections?.length ? ` (${p.connections.length} سرویس)` : ""}
                </option>
              ))}
            </select>
            {form.package_id && (
              <div className="text-xs text-gray-400 mt-1">
                حجم، مدت اعتبار و سرویس‌های این کاربر مستقیما از روی پکیج ساخته می‌شود.
                {!isSuperadmin && (() => {
                  const pkg = packages.find((p) => String(p.id) === String(form.package_id));
                  if (!pkg) return null;
                  const cost = pkg.cooperation_price != null ? pkg.cooperation_price : pkg.price;
                  return (
                    <div className="text-amber-600 mt-1">
                      {new Intl.NumberFormat("fa-IR").format(cost || 0)} تومان از اعتبار شما کم می‌شود.
                    </div>
                  );
                })()}
              </div>
            )}
          </div>

          {isSuperadmin && !form.package_id && (
            <>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-gray-600 mb-1">حجم مجاز (گیگابایت)</label>
                  <input
                    type="number"
                    step="0.1"
                    min="0"
                    className="input"
                    placeholder="0 = نامحدود"
                    value={form.quota_gb}
                    onChange={(e) => setForm({ ...form, quota_gb: e.target.value })}
                  />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">اتصال هم‌زمان (کل سرویس‌ها)</label>
                  <input
                    type="number"
                    min="0"
                    className="input"
                    placeholder="پیش‌فرض 1"
                    value={form.max_concurrent_sessions}
                    onChange={(e) => setForm({ ...form, max_concurrent_sessions: e.target.value })}
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-1">نوع انقضا</label>
                <select
                  className="input"
                  value={form.expire_mode}
                  onChange={(e) => setForm({ ...form, expire_mode: e.target.value })}
                >
                  <option value="none">بدون انقضا</option>
                  <option value="date">تاریخ مشخص</option>
                  <option value="days_from_now">تعداد روز از الان</option>
                  <option value="first_use">تعداد روز از اولین اتصال</option>
                </select>

                {form.expire_mode === "date" && (
                  <input
                    type="date"
                    className="input mt-2"
                    value={form.expire_at}
                    onChange={(e) => setForm({ ...form, expire_at: e.target.value })}
                  />
                )}

                {form.expire_mode === "days_from_now" && (
                  <input
                    type="number"
                    min="1"
                    className="input mt-2"
                    placeholder="مثلا 30"
                    value={form.expire_days}
                    onChange={(e) => setForm({ ...form, expire_days: e.target.value })}
                  />
                )}

                {form.expire_mode === "first_use" && (
                  <>
                    <input
                      type="number"
                      min="1"
                      className="input mt-2"
                      placeholder="مثلا 30"
                      value={form.expire_days}
                      onChange={(e) => setForm({ ...form, expire_days: e.target.value })}
                    />
                    <div className="text-xs text-gray-400 mt-1">
                      تا وقتی کاربر برای اولین بار وصل نشده انقضا فعال نمی‌شود؛ از لحظه اولین اتصال موفق، شمارش {form.expire_days || "N"} روز شروع می‌شود.
                    </div>
                  </>
                )}
              </div>
            </>
          )}

          <div>
            <label className="block text-sm text-gray-600 mb-1">یادداشت</label>
            <textarea className="input" rows={2} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </div>

          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              انصراف
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? "در حال ساخت..." : "ساخت کاربر"}
            </button>
          </div>
        </form>
      </Modal>

      {/* Bulk create modal */}
      <Modal open={bulkCreateOpen} onClose={() => setBulkCreateOpen(false)} title="ساخت گروهی کاربران" width="max-w-2xl">
        <form onSubmit={submitBulkCreate} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">پیشوند نام کاربری *</label>
              <input
                className="input"
                required
                value={bulkCreateForm.prefix}
                onChange={(e) => setBulkCreateForm((f) => ({ ...f, prefix: e.target.value }))}
              />
              <div className="text-xs text-gray-400 mt-1">
                نام‌ها به‌صورت {bulkCreateForm.prefix || "user"}1, {bulkCreateForm.prefix || "user"}2, ... ساخته می‌شوند
              </div>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">تعداد کاربر *</label>
              <input
                type="number"
                min="1"
                max="1000"
                required
                className="input"
                value={bulkCreateForm.count}
                onChange={(e) => setBulkCreateForm((f) => ({ ...f, count: e.target.value }))}
              />
            </div>
            <div className="col-span-2">
              <label className="block text-sm text-gray-600 mb-1">
                ساخت با پکیج{isSuperadmin ? " (اختیاری)" : ""}
              </label>
              <select
                className="input"
                required={!isSuperadmin}
                value={bulkCreateForm.package_id}
                onChange={(e) => setBulkCreateForm((f) => ({ ...f, package_id: e.target.value }))}
              >
                {isSuperadmin && <option value="">بدون پکیج (تنظیم دستی)</option>}
                {packages.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} — {p.quota_gb ? `${p.quota_gb}GB` : "نامحدود"} / {p.duration_days ? `${p.duration_days} روز` : "بدون انقضا"}
                    {p.connections?.length ? ` (${p.connections.length} سرویس)` : ""}
                  </option>
                ))}
              </select>
              {bulkCreateForm.package_id && (
                <div className="text-xs text-gray-400 mt-1">
                  حجم، مدت اعتبار و سرویس‌های هرکدام از کاربران مستقیما از روی پکیج ساخته می‌شود.
                  {!isSuperadmin && (() => {
                    const pkg = packages.find((p) => String(p.id) === String(bulkCreateForm.package_id));
                    if (!pkg) return null;
                    const unitCost = pkg.cooperation_price != null ? pkg.cooperation_price : pkg.price;
                    const count = Number(bulkCreateForm.count) || 0;
                    return (
                      <div className="text-amber-600 mt-1">
                        {new Intl.NumberFormat("fa-IR").format(unitCost || 0)} تومان × {count || "?"} کاربر ={" "}
                        {new Intl.NumberFormat("fa-IR").format((unitCost || 0) * count)} تومان از اعتبار شما کم می‌شود.
                      </div>
                    );
                  })()}
                </div>
              )}
            </div>
            {isSuperadmin && !bulkCreateForm.package_id && (
              <>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">حجم مجاز هرکدام (گیگابایت)</label>
                  <input
                    type="number"
                    step="0.1"
                    min="0"
                    className="input"
                    placeholder="0 = نامحدود"
                    value={bulkCreateForm.quota_gb}
                    onChange={(e) => setBulkCreateForm((f) => ({ ...f, quota_gb: e.target.value }))}
                  />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">اعتبار (روز)</label>
                  <input
                    type="number"
                    min="0"
                    className="input"
                    placeholder="خالی = بدون انقضا"
                    value={bulkCreateForm.expire_days}
                    onChange={(e) => setBulkCreateForm((f) => ({ ...f, expire_days: e.target.value }))}
                  />
                </div>
              </>
            )}
            <div className="col-span-2">
              <label className="block text-sm text-gray-600 mb-1">یادداشت (برای همه)</label>
              <input
                className="input"
                value={bulkCreateForm.notes}
                onChange={(e) => setBulkCreateForm((f) => ({ ...f, notes: e.target.value }))}
              />
            </div>
          </div>

          {isSuperadmin && !bulkCreateForm.package_id && (
          <div className="border-t border-gray-100 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-medium text-gray-700">اتصالات (اختیاری، برای هرکدام از کاربران ساخته می‌شود)</div>
              <button type="button" className="btn-secondary" onClick={addBulkConn}>
                <Plus size={14} /> افزودن اتصال
              </button>
            </div>
            {bulkCreateForm.connections.map((c, idx) => (
              <div key={idx} className="grid grid-cols-4 gap-2 mb-2 items-center">
                <select className="input col-span-2" value={c.node_id} onChange={(e) => updateBulkConn(idx, "node_id", e.target.value)}>
                  <option value="">انتخاب سرور...</option>
                  {nodes.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.name}
                    </option>
                  ))}
                </select>
                <select className="input" value={c.protocol} onChange={(e) => updateBulkConn(idx, "protocol", e.target.value)}>
                  <option value="wireguard">WireGuard</option>
                  <option value="openvpn">OpenVPN</option>
                  <option value="l2tp">L2TP</option>
                  <option value="ikev2">IKEv2</option>
                  <option value="xray">V2Ray/Xray</option>
                </select>
                <div className="flex items-center gap-1">
                  <input
                    type="number"
                    min="0"
                    className="input"
                    title="حداکثر اتصال هم‌زمان (فقط OpenVPN/L2TP/IKEv2)"
                    value={c.max_concurrent_sessions}
                    onChange={(e) => updateBulkConn(idx, "max_concurrent_sessions", e.target.value)}
                  />
                  <button type="button" className="text-gray-400 hover:text-red-600" onClick={() => removeBulkConn(idx)}>
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>
          )}

          {bulkCreateError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{bulkCreateError}</div>}
          {bulkCreateResult && (
            <div className="text-xs text-gray-600 bg-gray-50 rounded-lg px-3 py-2">
              {bulkCreateResult.created_count} کاربر ساخته شد، {bulkCreateResult.skipped_count} رد شد.
              {bulkCreateResult.skipped_count > 0 && (
                <ul className="mt-1 list-disc pr-4 space-y-0.5 max-h-32 overflow-y-auto">
                  {bulkCreateResult.skipped.map((s, i) => (
                    <li key={i}>
                      {s.name}: {s.reason}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setBulkCreateOpen(false)}>
              بستن
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? "در حال ساخت..." : "ساخت گروهی"}
            </button>
          </div>
        </form>
      </Modal>

      {/* Bulk edit modal */}
      <Modal open={bulkEditOpen} onClose={() => setBulkEditOpen(false)} title={`ویرایش گروهی (${selected.size} کاربر)`}>
        <form onSubmit={submitBulkEdit} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">افزودن حجم (گیگابایت)</label>
              <input
                type="number"
                step="0.1"
                min="0"
                className="input"
                placeholder="0"
                value={bulkEditForm.add_gb}
                onChange={(e) => setBulkEditForm((f) => ({ ...f, add_gb: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">افزودن اعتبار (روز)</label>
              <input
                type="number"
                min="0"
                className="input"
                placeholder="0"
                value={bulkEditForm.add_days}
                onChange={(e) => setBulkEditForm((f) => ({ ...f, add_days: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">وضعیت</label>
              <select
                className="input"
                value={bulkEditForm.status}
                onChange={(e) => setBulkEditForm((f) => ({ ...f, status: e.target.value }))}
              >
                <option value="">بدون تغییر</option>
                <option value="active">فعال</option>
                <option value="disabled">غیرفعال</option>
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">محدودیت اتصال هم‌زمان</label>
              <input
                type="number"
                min="0"
                className="input"
                placeholder="بدون تغییر"
                value={bulkEditForm.max_concurrent_sessions}
                onChange={(e) => setBulkEditForm((f) => ({ ...f, max_concurrent_sessions: e.target.value }))}
              />
            </div>
            <div className="col-span-2 flex items-center gap-2">
              <input
                type="checkbox"
                id="bulk_reset_usage"
                checked={bulkEditForm.reset_usage}
                onChange={(e) => setBulkEditForm((f) => ({ ...f, reset_usage: e.target.checked }))}
              />
              <label htmlFor="bulk_reset_usage" className="text-sm text-gray-600">
                ریست حجم مصرفی
              </label>
            </div>
          </div>
          {bulkEditError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{bulkEditError}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setBulkEditOpen(false)}>
              انصراف
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? "در حال ذخیره..." : "اعمال روی همه"}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
