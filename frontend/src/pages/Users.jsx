import React, { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Plus, Search, Trash2, RotateCcw, Network, Layers, PencilLine, ChevronRight, ChevronLeft, X, ArrowUpDown, FileDown, Wand2, CheckSquare } from "lucide-react";

// یوزرنیم رندوم برای دکمه "تولید خودکار" کاربر - فقط حروف/عدد لاتین (مشابه
// همون تابع تو Admins.jsx، اینجا مستقل تعریف شده چون دو صفحه جدان).
function randomUsername() {
  const alphabet = "abcdefghijkmnpqrstuvwxyz23456789";
  let out = "user";
  for (let i = 0; i < 6; i++) out += alphabet[Math.floor(Math.random() * alphabet.length)];
  return out;
}
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import QuotaBar from "../components/QuotaBar.jsx";
import {
  fetchUsers,
  fetchUserIds,
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
import { statusLabel, STATUS_STYLES, formatDate, gbToBytes, downloadBlob } from "../utils.js";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";

const PAGE_SIZE = 50;

const emptyBulkConn = { node_id: "", protocol: "openvpn", max_concurrent_sessions: 1 };

const STATUS_FILTER_OPTIONS = [
  { value: "", labelKey: "status.all" },
  { value: "active", labelKey: "status.active" },
  { value: "disabled", labelKey: "status.disabled" },
  { value: "quota_exceeded", labelKey: "status.quota_exceeded" },
  { value: "expired", labelKey: "status.expired" },
];

const SORT_OPTIONS = [
  { value: "id", labelKey: "sort.id" },
  { value: "username", labelKey: "sort.username" },
  { value: "used_bytes", labelKey: "sort.used_bytes" },
  { value: "total_quota_bytes", labelKey: "sort.total_quota_bytes" },
  { value: "expire_at", labelKey: "sort.expire_at" },
  { value: "status", labelKey: "sort.status" },
];

export default function Users() {
  const { isSuperadmin } = useAuth();
  const { t, language } = useLanguage();
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
  const [packageFilter, setPackageFilter] = useState("");
  // Same packages the create-forms use (`packages`, enabled-only) aren't
  // enough for filtering - a user may have been created from a package
  // that's since been disabled, and they'd become impossible to find/select
  // by package again. Keep a separate, unfiltered list just for the filter
  // dropdown and the bulk-edit "اعمال پکیج" selector below.
  const [allPackages, setAllPackages] = useState([]);
  const [selectingAll, setSelectingAll] = useState(false);

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
    package_id: "",
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
        packageId: packageFilter,
      });
      const filename =
        (res.headers["content-disposition"] || "").match(/filename="?([^"]+)"?/)?.[1] || "users_export.xlsx";
      downloadBlob(filename, res.data);
    } catch (err) {
      alert(t("users.exportError"));
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
      packageId: packageFilter,
    }).then((res) => {
      setUsers(res.data.items);
      setTotal(res.data.total);
    });

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, search, statusFilter, onlineOnly, sortBy, sortDir, ownerAdminFilter, packageFilter]);

  useEffect(() => {
    fetchNodes().then((res) => setNodes(res.data));
    fetchPackages().then((res) => {
      setPackages(res.data.filter((p) => p.enabled));
      setAllPackages(res.data);
    });
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
    const debounceTimer = setTimeout(() => {
      setPage(1);
      setSearch(searchInput.trim());
    }, 350);
    return () => clearTimeout(debounceTimer);
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
      setError(err?.response?.data?.detail || t("users.createUserError"));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (id) => {
    if (!confirm(t("users.confirmDeleteUser"))) return;
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

  // Selects every user matching the current filters (search/status/online/
  // admin/package) across ALL pages, not just the one on screen - so a
  // group action like "disable everyone on this package" actually covers
  // everyone, even with more than PAGE_SIZE matches.
  const selectAllMatching = async () => {
    setSelectingAll(true);
    try {
      const res = await fetchUserIds(search, {
        status: statusFilter,
        onlineOnly,
        ownerAdminId: ownerAdminFilter,
        packageId: packageFilter,
      });
      setSelected(new Set(res.data));
    } finally {
      setSelectingAll(false);
    }
  };

  const onBulkDelete = async () => {
    if (selected.size === 0) return;
    if (!confirm(t("users.confirmBulkDelete", { count: selected.size }))) return;
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
      setBulkCreateError(err?.response?.data?.detail || t("users.bulkCreateError"));
    } finally {
      setSaving(false);
    }
  };

  // ---------------- bulk edit ----------------
  const openBulkEdit = () => {
    setBulkEditForm({ add_gb: "", add_days: "", reset_usage: false, status: "", max_concurrent_sessions: "", package_id: "" });
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
        package_id: bulkEditForm.package_id ? Number(bulkEditForm.package_id) : null,
      };
      await bulkUpdateUsers(payload);
      setBulkEditOpen(false);
      clearSelection();
      load();
    } catch (err) {
      setBulkEditError(err?.response?.data?.detail || t("users.bulkEditError"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Layout>
      <Topbar title={t("users.title")} subtitle={t("users.subtitle", { count: total })} />

      <div className="card !p-4 mb-4 space-y-3">
        <div className="flex items-center gap-2 flex-wrap">
          <div className="relative">
            <Search className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" size={16} />
            <input
              className="input pr-9 !w-full sm:!w-56"
              placeholder={t("users.search")}
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
            />
          </div>

          <select
            className="input !w-auto min-w-[7.5rem] cursor-pointer"
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setPage(1);
            }}
          >
            {STATUS_FILTER_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {t(o.labelKey)}
              </option>
            ))}
          </select>

          <select
            className="input !w-auto min-w-[8rem] cursor-pointer"
            value={packageFilter}
            onChange={(e) => {
              setPackageFilter(e.target.value);
              setPage(1);
            }}
            title={t("users.filterByPackage")}
          >
            <option value="">{t("users.allPackages")}</option>
            {allPackages.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>

          <div className="flex items-center gap-1">
            <select
              className="input !w-auto min-w-[7rem] cursor-pointer"
              value={sortBy}
              onChange={(e) => {
                setSortBy(e.target.value);
                setPage(1);
              }}
              title={t("users.sortBy")}
            >
              {SORT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {t(o.labelKey)}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn-secondary !px-2.5"
              title={sortDir === "asc" ? t("users.ascending") : t("users.descending")}
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
              className="input !w-auto min-w-[7.5rem] cursor-pointer"
              value={ownerAdminFilter}
              onChange={(e) => {
                setOwnerAdminFilter(e.target.value);
                setPage(1);
              }}
              title={t("users.filterByAdmin")}
            >
              <option value="">{t("users.allAdmins")}</option>
              {admins.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.username}
                </option>
              ))}
            </select>
          )}

          {onlineOnly && (
            <span className="badge bg-emerald-50 text-emerald-600 inline-flex items-center gap-1">
              {t("users.onlineOnlyBadge")}
              <button type="button" className="hover:text-emerald-800" onClick={clearFilters}>
                <X size={12} />
              </button>
            </span>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 flex-wrap pt-3 border-t border-gray-100">
          <div className="flex items-center gap-2 flex-wrap">
            {(statusFilter || packageFilter || onlineOnly || ownerAdminFilter || search) && total > 0 && (
              <button className="btn-secondary" onClick={selectAllMatching} disabled={selectingAll}>
                <CheckSquare size={16} /> {selectingAll ? "..." : t("users.selectAllMatching", { count: total })}
              </button>
            )}
            {selected.size > 0 && (
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-medium text-brand-700 bg-brand-50 rounded-full px-2.5 py-1">
                  {t("users.selected", { count: selected.size })}
                </span>
                <button className="btn-secondary" onClick={openBulkEdit}>
                  <PencilLine size={16} /> {t("users.bulkEdit")}
                </button>
                <button className="btn-danger" onClick={onBulkDelete}>
                  <Trash2 size={16} /> {t("users.bulkDelete")}
                </button>
                <button className="btn-secondary" onClick={clearSelection}>
                  {t("users.clearSelection")}
                </button>
              </div>
            )}
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <button className="btn-secondary" onClick={onExport} disabled={exporting}>
              <FileDown size={16} /> {exporting ? "..." : t("users.exportExcel")}
            </button>
            <button className="btn-secondary" onClick={openBulkCreate}>
              <Layers size={16} /> {t("users.bulkCreate")}
            </button>
            <button className="btn-primary" onClick={() => setOpen(true)}>
              <Plus size={16} /> {t("users.newUser")}
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
              <th className="text-right font-medium px-4 py-3">{t("users.colUser")}</th>
              {isSuperadmin && <th className="text-right font-medium px-4 py-3">{t("users.colAdmin")}</th>}
              <th className="text-right font-medium px-4 py-3">{t("users.colStatus")}</th>
              <th className="text-right font-medium px-4 py-3 w-56">{t("users.colUsage")}</th>
              <th className="text-right font-medium px-4 py-3">{t("users.colConnections")}</th>
              <th className="text-right font-medium px-4 py-3">{t("users.colExpiry")}</th>
              <th className="text-right font-medium px-4 py-3">{t("users.colActions")}</th>
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
                      title={u.online ? t("users.online") : t("users.offline")}
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
                  <span className={`badge ${STATUS_STYLES[u.status]}`}>{statusLabel(u.status, language)}</span>
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
                    <span className="text-amber-600" title={t("users.notConnectedYet")}>
                      {t("users.fromFirstConnection", { days: u.expire_days_after_first_use })}
                    </span>
                  ) : (
                    formatDate(u.expire_at, language)
                  )}
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <button title={t("users.resetUsage")} onClick={() => onReset(u.id)} className="text-gray-400 hover:text-brand-600">
                      <RotateCcw size={16} />
                    </button>
                    <button title={t("common.delete")} onClick={() => onDelete(u.id)} className="text-gray-400 hover:text-red-600">
                      <Trash2 size={16} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={isSuperadmin ? 8 : 7} className="text-center text-gray-400 py-10">
                  {t("users.noUsers")}
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="flex items-center justify-between px-4 py-3 border-t border-gray-50 text-sm text-gray-500">
          <div>
            {t("users.page", { page, total: totalPages })}
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-secondary" disabled={page <= 1} onClick={() => setPage((p) => Math.max(p - 1, 1))}>
              <ChevronRight size={14} /> {t("users.prev")}
            </button>
            <button
              className="btn-secondary"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => Math.min(p + 1, totalPages))}
            >
              {t("users.next")} <ChevronLeft size={14} />
            </button>
          </div>
        </div>
      </div>

      {/* Single create modal */}
      <Modal open={open} onClose={() => setOpen(false)} title={t("users.newUserModalTitle")}>
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("users.fieldUsername")}</label>
            <div className="flex gap-2">
              <input
                className="input flex-1"
                dir="ltr"
                required
                value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
              />
              <button
                type="button"
                className="btn-secondary shrink-0"
                title={t("users.autoGenUsername")}
                onClick={() => setForm({ ...form, username: randomUsername() })}
              >
                <Wand2 size={16} />
              </button>
            </div>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("users.fieldFullName")}</label>
            <input className="input" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
          </div>

          {isSuperadmin && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("users.fieldOwnerAdmin")}</label>
              <select
                className="input"
                value={form.owner_admin_id}
                onChange={(e) => setForm({ ...form, owner_admin_id: e.target.value })}
              >
                <option value="">{t("users.myselfMainAdmin")}</option>
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
              {t("users.createWithPackage")}{isSuperadmin ? t("users.optional") : ""}
            </label>
            <select
              className="input"
              required={!isSuperadmin}
              value={form.package_id}
              onChange={(e) => setForm({ ...form, package_id: e.target.value })}
            >
              {isSuperadmin && <option value="">{t("users.noPackageManual")}</option>}
              {packages.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} — {p.quota_gb ? `${p.quota_gb}GB` : t("users.unlimited")} / {p.duration_days ? t("users.daysUnit", { days: p.duration_days }) : t("users.noExpiry")}
                  {p.connections?.length ? t("users.servicesCount", { count: p.connections.length }) : ""}
                </option>
              ))}
            </select>
            {form.package_id && (
              <div className="text-xs text-gray-400 mt-1">
                {t("users.packageDerivedHint")}
                {!isSuperadmin && (() => {
                  const pkg = packages.find((p) => String(p.id) === String(form.package_id));
                  if (!pkg) return null;
                  const cost = pkg.cooperation_price != null ? pkg.cooperation_price : pkg.price;
                  return (
                    <div className="text-amber-600 mt-1">
                      {t("users.packageCostDeduction", { cost: new Intl.NumberFormat("fa-IR").format(cost || 0) })}
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
                  <label className="block text-sm text-gray-600 mb-1">{t("users.fieldQuota")}</label>
                  <input
                    type="number"
                    step="0.1"
                    min="0"
                    className="input"
                    placeholder={t("users.quotaPlaceholder")}
                    value={form.quota_gb}
                    onChange={(e) => setForm({ ...form, quota_gb: e.target.value })}
                  />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("users.fieldMaxConcurrent")}</label>
                  <input
                    type="number"
                    min="0"
                    className="input"
                    placeholder={t("users.maxConcurrentPlaceholder")}
                    value={form.max_concurrent_sessions}
                    onChange={(e) => setForm({ ...form, max_concurrent_sessions: e.target.value })}
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-1">{t("users.fieldExpireType")}</label>
                <select
                  className="input"
                  value={form.expire_mode}
                  onChange={(e) => setForm({ ...form, expire_mode: e.target.value })}
                >
                  <option value="none">{t("users.expireNone")}</option>
                  <option value="date">{t("users.expireDate")}</option>
                  <option value="days_from_now">{t("users.expireDaysFromNow")}</option>
                  <option value="first_use">{t("users.expireFirstUse")}</option>
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
                    placeholder={t("users.daysPlaceholder")}
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
                      placeholder={t("users.daysPlaceholder")}
                      value={form.expire_days}
                      onChange={(e) => setForm({ ...form, expire_days: e.target.value })}
                    />
                    <div className="text-xs text-gray-400 mt-1">
                      {t("users.firstUseHint", { days: form.expire_days || "N" })}
                    </div>
                  </>
                )}
              </div>
            </>
          )}

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("users.fieldNotes")}</label>
            <textarea className="input" rows={2} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </div>

          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              {t("common.cancel")}
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? t("users.creatingUser") : t("users.createUserButton")}
            </button>
          </div>
        </form>
      </Modal>

      {/* Bulk create modal */}
      <Modal open={bulkCreateOpen} onClose={() => setBulkCreateOpen(false)} title={t("users.bulkCreateModalTitle")} width="max-w-2xl">
        <form onSubmit={submitBulkCreate} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("users.fieldUsernamePrefix")}</label>
              <input
                className="input"
                required
                value={bulkCreateForm.prefix}
                onChange={(e) => setBulkCreateForm((f) => ({ ...f, prefix: e.target.value }))}
              />
              <div className="text-xs text-gray-400 mt-1">
                {t("users.usernamePreviewHint", { prefix: bulkCreateForm.prefix || "user" })}
              </div>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("users.fieldUserCount")}</label>
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
                {t("users.createWithPackage")}{isSuperadmin ? t("users.optional") : ""}
              </label>
              <select
                className="input"
                required={!isSuperadmin}
                value={bulkCreateForm.package_id}
                onChange={(e) => setBulkCreateForm((f) => ({ ...f, package_id: e.target.value }))}
              >
                {isSuperadmin && <option value="">{t("users.noPackageManual")}</option>}
                {packages.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} — {p.quota_gb ? `${p.quota_gb}GB` : t("users.unlimited")} / {p.duration_days ? t("users.daysUnit", { days: p.duration_days }) : t("users.noExpiry")}
                    {p.connections?.length ? t("users.servicesCount", { count: p.connections.length }) : ""}
                  </option>
                ))}
              </select>
              {bulkCreateForm.package_id && (
                <div className="text-xs text-gray-400 mt-1">
                  {t("users.packageDerivedHintPlural")}
                  {!isSuperadmin && (() => {
                    const pkg = packages.find((p) => String(p.id) === String(bulkCreateForm.package_id));
                    if (!pkg) return null;
                    const unitCost = pkg.cooperation_price != null ? pkg.cooperation_price : pkg.price;
                    const count = Number(bulkCreateForm.count) || 0;
                    return (
                      <div className="text-amber-600 mt-1">
                        {t("users.packageCostDeductionPlural", {
                          unitCost: new Intl.NumberFormat("fa-IR").format(unitCost || 0),
                          count: count || "?",
                          total: new Intl.NumberFormat("fa-IR").format((unitCost || 0) * count),
                        })}
                      </div>
                    );
                  })()}
                </div>
              )}
            </div>
            {isSuperadmin && !bulkCreateForm.package_id && (
              <>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("users.fieldQuotaEach")}</label>
                  <input
                    type="number"
                    step="0.1"
                    min="0"
                    className="input"
                    placeholder={t("users.quotaPlaceholder")}
                    value={bulkCreateForm.quota_gb}
                    onChange={(e) => setBulkCreateForm((f) => ({ ...f, quota_gb: e.target.value }))}
                  />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("users.fieldExpireDays")}</label>
                  <input
                    type="number"
                    min="0"
                    className="input"
                    placeholder={t("users.expireDaysPlaceholder")}
                    value={bulkCreateForm.expire_days}
                    onChange={(e) => setBulkCreateForm((f) => ({ ...f, expire_days: e.target.value }))}
                  />
                </div>
              </>
            )}
            <div className="col-span-2">
              <label className="block text-sm text-gray-600 mb-1">{t("users.fieldNotesAll")}</label>
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
              <div className="text-sm font-medium text-gray-700">{t("users.connectionsOptionalHeading")}</div>
              <button type="button" className="btn-secondary" onClick={addBulkConn}>
                <Plus size={14} /> {t("users.addConnectionButton")}
              </button>
            </div>
            {bulkCreateForm.connections.map((c, idx) => (
              <div key={idx} className="grid grid-cols-4 gap-2 mb-2 items-center">
                <select className="input col-span-2" value={c.node_id} onChange={(e) => updateBulkConn(idx, "node_id", e.target.value)}>
                  <option value="">{t("users.selectServerPlaceholder")}</option>
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
                  <option value="sstp">SSTP</option>
                  <option value="xray">V2Ray/Xray</option>
                </select>
                <div className="flex items-center gap-1">
                  <input
                    type="number"
                    min="0"
                    className="input"
                    title={t("users.maxConcurrentTitle")}
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
              {t("users.bulkCreateResultSummary", { created: bulkCreateResult.created_count, skipped: bulkCreateResult.skipped_count })}
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
              {t("users.close")}
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? t("users.creatingUser") : t("users.createGroupButton")}
            </button>
          </div>
        </form>
      </Modal>

      {/* Bulk edit modal */}
      <Modal open={bulkEditOpen} onClose={() => setBulkEditOpen(false)} title={t("users.bulkEditModalTitle", { count: selected.size })}>
        <form onSubmit={submitBulkEdit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("users.renewWithPackage")}</label>
            <select
              className="input"
              value={bulkEditForm.package_id}
              onChange={(e) => setBulkEditForm((f) => ({ ...f, package_id: e.target.value }))}
            >
              <option value="">{t("users.noChangeUseFieldsBelow")}</option>
              {allPackages.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} — {p.quota_gb ? `${p.quota_gb}GB` : t("users.unlimited")} / {p.duration_days ? t("users.daysUnit", { days: p.duration_days }) : t("users.noExpiry")}
                </option>
              ))}
            </select>
            {bulkEditForm.package_id && (
              <div className="text-xs text-amber-600 mt-1">
                {t("users.packageOverwriteWarning")}
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("users.fieldAddGb")}</label>
              <input
                type="number"
                step="0.1"
                min="0"
                className="input"
                placeholder="0"
                disabled={!!bulkEditForm.package_id}
                value={bulkEditForm.add_gb}
                onChange={(e) => setBulkEditForm((f) => ({ ...f, add_gb: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("users.fieldAddDays")}</label>
              <input
                type="number"
                min="0"
                className="input"
                placeholder="0"
                disabled={!!bulkEditForm.package_id}
                value={bulkEditForm.add_days}
                onChange={(e) => setBulkEditForm((f) => ({ ...f, add_days: e.target.value }))}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("users.fieldStatus")}</label>
              <select
                className="input"
                value={bulkEditForm.status}
                onChange={(e) => setBulkEditForm((f) => ({ ...f, status: e.target.value }))}
              >
                <option value="">{t("users.noChange")}</option>
                <option value="active">{t("status.active")}</option>
                <option value="disabled">{t("status.disabled")}</option>
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("users.fieldConcurrentLimit")}</label>
              <input
                type="number"
                min="0"
                className="input"
                placeholder={t("userDetail.noChangePlaceholder")}
                disabled={!!bulkEditForm.package_id}
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
                {t("users.resetUsageCheckbox")}
              </label>
            </div>
          </div>
          {bulkEditError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{bulkEditError}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setBulkEditOpen(false)}>
              {t("common.cancel")}
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? t("users.savingEllipsis") : t("users.applyToAll")}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
