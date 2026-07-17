import React, { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, ShieldCheck, Users as UsersIcon, Link2, Wallet, Send, Wand2, Eye, EyeOff, UsersRound, History, TrendingUp, TrendingDown, MapPin, CheckCircle2, XCircle, Database, Server } from "lucide-react";
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
  topupAdminBalance,
  fetchAdminBalanceLogs,
  fetchAdminLoginLogs,
  topupAdminVolume,
  fetchAdminVolumeLogs,
  fetchAvailableNodesForGrant,
  setAdminNodes,
  reparentAdmin,
} from "../api/client.js";
import { formatDateTime } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";
import { useAuth } from "../context/AuthContext.jsx";

function formatToman(n) {
  return new Intl.NumberFormat("fa-IR").format(n || 0);
}
function formatGb(n) {
  return new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 2 }).format(n || 0);
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

const emptyForm = {
  username: "",
  password: "",
  permissions: [],
  login_slug: "",
  balance: "",
  telegram_id: "",
  group_id: "",
  initial_balance: "",
  billing_mode: "flat",
  volume_balance_gb: 0,
  initial_volume_gb: "",
  // Superadmin-only, only used at CREATE time - "" = level-2 Admin, or an
  // existing level-2 Admin's id = create straight as their Seller (see
  // schemas.AdminCreate.parent_admin_id). Existing-account role changes go
  // through the separate reparent control instead (see roleParentId state).
  parent_admin_id: "",
};
const emptyGroupForm = { name: "", permissions: [] };

export default function Admins() {
  const { t, language } = useLanguage();
  const { isSuperadmin, adminId } = useAuth();
  const [items, setItems] = useState([]);
  const [choices, setChoices] = useState({});
  const [permGroups, setPermGroups] = useState({});
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [editingRole, setEditingRole] = useState(null); // "admin" | "seller" - which tier the modal is currently editing
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  // ---------- Node access assignment (سوپر ادمین -> ادمین سطح ۲، مورد ۳/۷ لیست تسک‌ها) ----------
  const [availableNodes, setAvailableNodes] = useState(null); // null = not loaded yet
  const [selectedNodeIds, setSelectedNodeIds] = useState([]);
  const [nodesSaving, setNodesSaving] = useState(false);
  const [nodesError, setNodesError] = useState("");
  const [nodesSaved, setNodesSaved] = useState(false);

  // ---------- Role reassignment: Admin <-> Seller (superadmin only) ----------
  // "" = level-2 Admin, or an existing Admin's id = Seller under them.
  const [roleParentId, setRoleParentId] = useState("");
  const [roleSaving, setRoleSaving] = useState(false);
  const [roleError, setRoleError] = useState("");
  const [roleSaved, setRoleSaved] = useState(false);

  const [groups, setGroups] = useState([]);
  const [groupOpen, setGroupOpen] = useState(false);
  const [editingGroupId, setEditingGroupId] = useState(null);
  const [groupForm, setGroupForm] = useState(emptyGroupForm);
  const [groupError, setGroupError] = useState("");
  const [groupSaving, setGroupSaving] = useState(false);

  // ---------- Balance top-up / audit log (مورد ۴ از لیست ویژگی‌ها) ----------
  const [topupAmount, setTopupAmount] = useState("");
  const [topupNote, setTopupNote] = useState("");
  const [topupSaving, setTopupSaving] = useState(false);
  const [topupError, setTopupError] = useState("");
  const [showLogs, setShowLogs] = useState(false);
  const [balanceLogs, setBalanceLogs] = useState([]);
  const [logsLoading, setLogsLoading] = useState(false);

  // ---------- Volume top-up / audit log (مورد ۶ - حالت حجمی) ----------
  const [topupVolumeAmount, setTopupVolumeAmount] = useState("");
  const [topupVolumeNote, setTopupVolumeNote] = useState("");
  const [topupVolumeSaving, setTopupVolumeSaving] = useState(false);
  const [topupVolumeError, setTopupVolumeError] = useState("");
  const [showVolumeLogs, setShowVolumeLogs] = useState(false);
  const [volumeLogs, setVolumeLogs] = useState([]);
  const [volumeLogsLoading, setVolumeLogsLoading] = useState(false);

  // ---------- Admin login report (مورد ۵ از لیست ویژگی‌ها) ----------
  const [loginLogsOpen, setLoginLogsOpen] = useState(false);
  const [loginLogs, setLoginLogs] = useState([]);
  const [loginLogsLoading, setLoginLogsLoading] = useState(false);
  const [loginLogFilterAdmin, setLoginLogFilterAdmin] = useState("");
  const [loginLogOnlyFailed, setLoginLogOnlyFailed] = useState(false);

  const load = () => fetchAdmins().then((res) => setItems(res.data));
  const loadGroups = () => fetchAdminGroups().then((res) => setGroups(res.data));
  useEffect(() => {
    load();
    loadGroups();
    fetchPermissionChoices().then((res) => {
      setChoices(res.data.choices || res.data);
      setPermGroups(res.data.groups || {});
    });
  }, []);

  const loadLoginLogs = () => {
    setLoginLogsLoading(true);
    fetchAdminLoginLogs({
      admin_id: loginLogFilterAdmin || undefined,
      only_failed: loginLogOnlyFailed || undefined,
      limit: 200,
    })
      .then((res) => setLoginLogs(res.data))
      .finally(() => setLoginLogsLoading(false));
  };

  const toggleLoginLogs = () => {
    const next = !loginLogsOpen;
    setLoginLogsOpen(next);
    if (next) loadLoginLogs();
  };

  useEffect(() => {
    if (loginLogsOpen) loadLoginLogs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loginLogFilterAdmin, loginLogOnlyFailed]);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const togglePerm = (key) =>
    setForm((f) => ({
      ...f,
      permissions: f.permissions.includes(key) ? f.permissions.filter((p) => p !== key) : [...f.permissions, key],
    }));

  const resetTopupState = () => {
    setTopupAmount("");
    setTopupNote("");
    setTopupError("");
    setShowLogs(false);
    setBalanceLogs([]);
    setTopupVolumeAmount("");
    setTopupVolumeNote("");
    setTopupVolumeError("");
    setShowVolumeLogs(false);
    setVolumeLogs([]);
  };

  const resetNodeAssignState = () => {
    setSelectedNodeIds([]);
    setNodesError("");
    setNodesSaved(false);
  };

  const openCreate = () => {
    setEditingId(null);
    setEditingRole(null);
    setForm(emptyForm);
    setError("");
    setShowPassword(false);
    resetTopupState();
    resetNodeAssignState();
    setOpen(true);
  };

  const openEdit = (admin) => {
    setEditingId(admin.id);
    setEditingRole(admin.role || (admin.is_superadmin ? "superadmin" : "seller"));
    setForm({
      username: admin.username,
      password: "",
      permissions: admin.permissions || [],
      login_slug: admin.login_slug || "",
      balance: admin.balance || 0,
      telegram_id: admin.telegram_id || "",
      group_id: admin.group_id || "",
      initial_balance: "",
      billing_mode: admin.billing_mode || "flat",
      volume_balance_gb: admin.volume_balance_gb || 0,
      initial_volume_gb: "",
    });
    setError("");
    resetTopupState();
    resetNodeAssignState();
    // Node assignment only makes sense for a level-2 Admin, edited by a
    // superadmin (see routers/admins.py's set_admin_nodes) - a Seller never
    // gets direct node access (services/hierarchy.py), and a level-2 Admin
    // editing their own Seller has no node-assignment power at all.
    if (isSuperadmin && admin.role === "admin") {
      setSelectedNodeIds(admin.accessible_node_ids || []);
      if (availableNodes === null) {
        fetchAvailableNodesForGrant().then((res) => setAvailableNodes(res.data));
      }
    }
    // Role reassignment (superadmin editing anyone but themselves/another
    // superadmin) - starts at this account's CURRENT parent, so "save" is
    // a no-op unless something's actually changed.
    setRoleParentId(admin.parent_admin_id || "");
    setRoleError("");
    setRoleSaved(false);
    setOpen(true);
  };

  const toggleNodeSelected = (nodeId) =>
    setSelectedNodeIds((ids) => (ids.includes(nodeId) ? ids.filter((id) => id !== nodeId) : [...ids, nodeId]));

  const saveNodeAssignment = async () => {
    setNodesSaving(true);
    setNodesError("");
    setNodesSaved(false);
    try {
      await setAdminNodes(editingId, selectedNodeIds);
      setNodesSaved(true);
      load();
    } catch (err) {
      setNodesError(err?.response?.data?.detail || t("admins.nodeAssignError"));
    } finally {
      setNodesSaving(false);
    }
  };

  const saveRole = async () => {
    setRoleSaving(true);
    setRoleError("");
    setRoleSaved(false);
    try {
      await reparentAdmin(editingId, roleParentId ? Number(roleParentId) : null);
      setRoleSaved(true);
      load();
    } catch (err) {
      setRoleError(err?.response?.data?.detail || t("admins.roleChangeError"));
    } finally {
      setRoleSaving(false);
    }
  };

  const doTopup = async () => {
    const amount = Number(topupAmount);
    if (!amount) {
      setTopupError(t("admins.amountRequired"));
      return;
    }
    setTopupSaving(true);
    setTopupError("");
    try {
      const res = await topupAdminBalance(editingId, { amount, note: topupNote || null });
      setForm((f) => ({ ...f, balance: res.data.balance }));
      setTopupAmount("");
      setTopupNote("");
      load();
      if (showLogs) loadLogs();
    } catch (err) {
      setTopupError(err?.response?.data?.detail || t("admins.balanceError"));
    } finally {
      setTopupSaving(false);
    }
  };

  const loadLogs = () => {
    setLogsLoading(true);
    fetchAdminBalanceLogs(editingId)
      .then((res) => setBalanceLogs(res.data))
      .finally(() => setLogsLoading(false));
  };

  const toggleLogs = () => {
    const next = !showLogs;
    setShowLogs(next);
    if (next) loadLogs();
  };

  const doVolumeTopup = async () => {
    const amount = Number(topupVolumeAmount);
    if (!amount) {
      setTopupVolumeError(t("admins.volumeAmountRequired"));
      return;
    }
    setTopupVolumeSaving(true);
    setTopupVolumeError("");
    try {
      const res = await topupAdminVolume(editingId, { amount_gb: amount, note: topupVolumeNote || null });
      setForm((f) => ({ ...f, volume_balance_gb: res.data.volume_balance_gb }));
      setTopupVolumeAmount("");
      setTopupVolumeNote("");
      load();
      if (showVolumeLogs) loadVolumeLogs();
    } catch (err) {
      setTopupVolumeError(err?.response?.data?.detail || t("admins.volumeError"));
    } finally {
      setTopupVolumeSaving(false);
    }
  };

  const loadVolumeLogs = () => {
    setVolumeLogsLoading(true);
    fetchAdminVolumeLogs(editingId)
      .then((res) => setVolumeLogs(res.data))
      .finally(() => setVolumeLogsLoading(false));
  };

  const toggleVolumeLogs = () => {
    const next = !showVolumeLogs;
    setShowVolumeLogs(next);
    if (next) loadVolumeLogs();
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
          billing_mode: form.billing_mode,
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
          initial_balance: form.initial_balance === "" ? null : Number(form.initial_balance),
          billing_mode: form.billing_mode,
          initial_volume_gb: form.initial_volume_gb === "" ? null : Number(form.initial_volume_gb),
          parent_admin_id: isSuperadmin && form.parent_admin_id ? Number(form.parent_admin_id) : null,
        });
      }
      setOpen(false);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || t("admins.saveError"));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (admin) => {
    if (!confirm(t("admins.deleteConfirm", { name: admin.username }))) return;
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
      setGroupError(err?.response?.data?.detail || t("admins.saveGroupError"));
    } finally {
      setGroupSaving(false);
    }
  };

  const onDeleteGroup = async (g) => {
    if (!confirm(t("admins.deleteGroupConfirm", { name: g.name }))) return;
    await deleteAdminGroup(g.id);
    loadGroups();
    load();
  };

  return (
    <Layout>
      <Topbar title={t("admins.title")} subtitle={t("admins.subtitle")} />

      <div className="card mb-6">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2 font-medium text-gray-700">
            <UsersRound size={16} className="text-violet-500" /> {t("admins.groupsHeading")}
          </div>
          <button type="button" className="btn-secondary" onClick={openGroupCreate}>
            <Plus size={14} /> {t("admins.newGroup")}
          </button>
        </div>
        {groups.length === 0 ? (
          <div className="text-sm text-gray-400">
            {t("admins.noGroups")}
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            {groups.map((g) => (
              <div key={g.id} className="flex items-center gap-2 bg-gray-50 rounded-xl px-3 py-2 text-sm">
                <span className="font-medium text-gray-700">{g.name}</span>
                <span className="text-xs text-gray-400">
                  ({g.permissions?.length ? g.permissions.map((p) => choices[p] || p).join("، ") : t("admins.onlyUsers")})
                </span>
                <span className="text-xs text-gray-400">· {t("admins.adminsCount", { count: g.admins_count })}</span>
                <button title={t("admins.editGroup")} onClick={() => openGroupEdit(g)} className="text-gray-400 hover:text-brand-600">
                  <Pencil size={14} />
                </button>
                <button title={t("admins.deleteGroup")} onClick={() => onDeleteGroup(g)} className="text-gray-400 hover:text-red-600">
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="flex justify-end mb-4">
        <button className="btn-primary" onClick={openCreate}>
          <Plus size={16} /> {t("admins.newAdmin")}
        </button>
      </div>

      <div className="card !p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 text-xs">
            <tr>
              <th className="text-right font-medium px-4 py-3">{t("admins.colUsername")}</th>
              <th className="text-right font-medium px-4 py-3">{t("admins.colRole")}</th>
              <th className="text-right font-medium px-4 py-3">{t("admins.colPermissions")}</th>
              <th className="text-right font-medium px-4 py-3">{t("admins.colUsersCount")}</th>
              <th className="text-right font-medium px-4 py-3">{t("admins.colBalance")}</th>
              <th className="text-right font-medium px-4 py-3">{t("admins.colTelegramBot")}</th>
              <th className="text-right font-medium px-4 py-3">{t("admins.colLoginLink")}</th>
              <th className="text-right font-medium px-4 py-3">{t("admins.colActions")}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((a) => (
              <tr key={a.id} className="border-t border-gray-50 hover:bg-gray-50/60">
                <td className="px-4 py-3 font-medium text-gray-800">{a.username}</td>
                <td className="px-4 py-3">
                  {a.is_superadmin ? (
                    <span className="badge bg-brand-50 text-brand-600 flex items-center gap-1 w-fit">
                      <ShieldCheck size={12} /> {t("admins.mainAdmin")}
                    </span>
                  ) : a.role === "admin" ? (
                    <span className="badge bg-violet-50 text-violet-600 flex items-center gap-1 w-fit">
                      <ShieldCheck size={12} /> {t("admins.roleAdmin")}
                    </span>
                  ) : (
                    <div className="flex flex-col gap-0.5 w-fit">
                      <span className="badge bg-gray-100 text-gray-500 w-fit">{t("admins.roleSeller")}</span>
                      {a.parent_admin_username && (
                        <span className="text-[11px] text-gray-400">
                          {t("admins.parentAdminLabel", { name: a.parent_admin_username })}
                        </span>
                      )}
                    </div>
                  )}
                </td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  {a.is_superadmin ? (
                    t("admins.everything")
                  ) : a.group_name ? (
                    <span className="badge bg-violet-50 text-violet-600 flex items-center gap-1 w-fit">
                      <UsersRound size={12} /> {t("admins.groupLabel", { name: a.group_name })}
                    </span>
                  ) : a.permissions?.length ? (
                    a.permissions.map((p) => choices[p] || p).join("، ")
                  ) : (
                    t("admins.onlyUsers")
                  )}
                </td>
                <td className="px-4 py-3 text-gray-600">
                  <span className="flex items-center gap-1">
                    <UsersIcon size={13} className="text-gray-400" /> {a.users_count}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-600" dir="ltr">
                  {!a.is_superadmin &&
                    (a.billing_mode === "usage" ? (
                      <span className="flex items-center gap-1 justify-end">
                        <Database size={12} className="text-violet-400" /> {formatGb(a.volume_balance_gb)} GB
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 justify-end">
                        <Wallet size={12} className="text-gray-400" /> {formatToman(a.balance)}
                      </span>
                    ))}
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
                      <button title={t("admins.editTitle")} onClick={() => openEdit(a)} className="text-gray-400 hover:text-brand-600">
                        <Pencil size={16} />
                      </button>
                      <button title={t("admins.deleteTitle")} onClick={() => onDelete(a)} className="text-gray-400 hover:text-red-600">
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
                  {t("admins.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="card mt-6">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2 font-medium text-gray-700">
            <MapPin size={16} className="text-brand-500" /> {t("admins.loginReportHeading")}
          </div>
          <button type="button" className="btn-secondary" onClick={toggleLoginLogs}>
            <History size={14} /> {loginLogsOpen ? t("admins.hideReport") : t("admins.viewReport")}
          </button>
        </div>

        {loginLogsOpen && (
          <>
            <div className="flex flex-wrap items-center gap-3 mb-3">
              <select className="input !w-auto" value={loginLogFilterAdmin} onChange={(e) => setLoginLogFilterAdmin(e.target.value)}>
                <option value="">{t("admins.allAdminsIncludingMain")}</option>
                {items.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.username}
                  </option>
                ))}
              </select>
              <label className="flex items-center gap-1.5 text-sm text-gray-600">
                <input type="checkbox" checked={loginLogOnlyFailed} onChange={(e) => setLoginLogOnlyFailed(e.target.checked)} />
                {t("admins.onlyFailedLogins")}
              </label>
            </div>

            <div className="border border-gray-100 rounded-xl overflow-hidden">
              {loginLogsLoading ? (
                <div className="text-sm text-gray-400 text-center py-6">{t("common.loading")}</div>
              ) : loginLogs.length === 0 ? (
                <div className="text-sm text-gray-400 text-center py-6">{t("admins.noResults")}</div>
              ) : (
                <div className="max-h-96 overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="bg-gray-50 text-gray-500 sticky top-0">
                      <tr>
                        <th className="text-right font-medium px-3 py-2">{t("admins.colTime")}</th>
                        <th className="text-right font-medium px-3 py-2">{t("admins.colUsernameShort")}</th>
                        <th className="text-right font-medium px-3 py-2">{t("admins.colIp")}</th>
                        <th className="text-right font-medium px-3 py-2">{t("admins.colStatus")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {loginLogs.map((l) => (
                        <tr key={l.id} className="border-t border-gray-50">
                          <td className="px-3 py-2 text-gray-500" dir="ltr">
                            {formatDateTime(l.created_at, language)}
                          </td>
                          <td className="px-3 py-2 text-gray-700">
                            {l.admin_username || l.attempted_username || "—"}
                            {l.admin_username && l.admin_username !== l.attempted_username && l.attempted_username && (
                              <span className="text-gray-400"> ({l.attempted_username})</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-gray-500" dir="ltr">
                            {l.ip_address || "—"}
                          </td>
                          <td className="px-3 py-2">
                            {l.success ? (
                              <span className="flex items-center gap-1 text-emerald-600">
                                <CheckCircle2 size={13} /> {t("admins.success")}
                              </span>
                            ) : (
                              <span className="flex items-center gap-1 text-red-500">
                                <XCircle size={13} /> {t("admins.failed")}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      <Modal open={open} onClose={() => setOpen(false)} title={editingId ? t("admins.editModal") : t("admins.newModal")} width="max-w-lg">
        <form onSubmit={submit} className="space-y-4">
          {!editingId && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("admins.fieldUsername")}</label>
              <div className="flex gap-2">
                <input className="input flex-1" required dir="ltr" value={form.username} onChange={(e) => set("username", e.target.value)} />
                <button
                  type="button"
                  className="btn-secondary shrink-0"
                  title={t("admins.generateUsername")}
                  onClick={() => set("username", generateUsername())}
                >
                  <Wand2 size={16} />
                </button>
              </div>
            </div>
          )}
          <div>
            <label className="block text-sm text-gray-600 mb-1">
              {editingId ? t("admins.newPasswordOptional") : t("admins.fieldPassword")}
            </label>
            <div className="flex gap-2">
              <input
                type={showPassword ? "text" : "password"}
                className="input flex-1"
                dir="ltr"
                required={!editingId}
                placeholder={editingId ? t("admins.emptyMeansNoChange") : ""}
                value={form.password}
                onChange={(e) => set("password", e.target.value)}
              />
              <button
                type="button"
                className="btn-secondary shrink-0"
                title={showPassword ? t("admins.hidePassword") : t("admins.showPassword")}
                onClick={() => setShowPassword((s) => !s)}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
              <button
                type="button"
                className="btn-secondary shrink-0"
                title={t("admins.generatePassword")}
                onClick={() => {
                  set("password", generatePassword());
                  setShowPassword(true);
                }}
              >
                <Wand2 size={16} />
              </button>
            </div>
          </div>

          {!editingId && isSuperadmin && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("admins.fieldRole")}</label>
              <select className="input" value={form.parent_admin_id} onChange={(e) => set("parent_admin_id", e.target.value)}>
                <option value="">{t("admins.roleAdmin")}</option>
                {adminId && <option value={adminId}>{t("admins.roleSellerUnderSuperadmin")}</option>}
                {items
                  .filter((a) => a.role === "admin")
                  .map((a) => (
                    <option key={a.id} value={a.id}>
                      {t("admins.roleSellerUnder", { name: a.username })}
                    </option>
                  ))}
              </select>
              <div className="text-xs text-gray-400 mt-1">{t("admins.fieldRoleHint")}</div>
            </div>
          )}

          {editingId && isSuperadmin && editingRole !== "superadmin" && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("admins.changeRoleLabel")}</label>
              <div className="flex gap-2">
                <select className="input flex-1" value={roleParentId} onChange={(e) => setRoleParentId(e.target.value)}>
                  <option value="">{t("admins.roleAdmin")}</option>
                  {adminId && <option value={adminId}>{t("admins.roleSellerUnderSuperadmin")}</option>}
                  {items
                    .filter((a) => a.role === "admin" && a.id !== editingId)
                    .map((a) => (
                      <option key={a.id} value={a.id}>
                        {t("admins.roleSellerUnder", { name: a.username })}
                      </option>
                    ))}
                </select>
                <button type="button" className="btn-secondary shrink-0" disabled={roleSaving} onClick={saveRole}>
                  {roleSaving ? t("common.saving") : t("admins.saveRole")}
                </button>
              </div>
              <div className="text-xs text-gray-400 mt-1">{t("admins.changeRoleHint")}</div>
              {roleSaved && <div className="text-xs text-emerald-600 mt-1">{t("admins.roleChangeSaved")}</div>}
              {roleError && <div className="text-xs text-red-500 mt-1">{roleError}</div>}
            </div>
          )}

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("admins.fieldGroup")}</label>
            <select className="input" value={form.group_id} onChange={(e) => set("group_id", e.target.value)}>
              <option value="">{t("admins.noGroupManual")}</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
            <div className="text-xs text-gray-400 mt-1">
              {t("admins.groupHint")}
            </div>
          </div>

          {editingId && isSuperadmin && editingRole === "admin" && (
            <div>
              <label className="flex items-center gap-1.5 text-sm text-gray-600 mb-2">
                <Server size={14} className="text-brand-500" /> {t("admins.nodeAssignHeading")}
              </label>
              <div className="text-xs text-gray-400 mb-2">{t("admins.nodeAssignHint")}</div>
              {availableNodes === null ? (
                <div className="text-sm text-gray-400 text-center py-4">{t("common.loading")}</div>
              ) : availableNodes.length === 0 ? (
                <div className="text-sm text-gray-400 text-center py-4">{t("admins.noNodesAvailable")}</div>
              ) : (
                <div className="border border-gray-100 rounded-xl max-h-48 overflow-y-auto p-2 space-y-1">
                  {availableNodes.map((node) => (
                    <label key={node.id} className="flex items-center gap-2 text-sm text-gray-600 px-1 py-1">
                      <input
                        type="checkbox"
                        checked={selectedNodeIds.includes(node.id)}
                        onChange={() => toggleNodeSelected(node.id)}
                      />
                      {node.name}
                    </label>
                  ))}
                </div>
              )}
              <div className="flex items-center gap-2 mt-2">
                <button type="button" className="btn-secondary" disabled={nodesSaving} onClick={saveNodeAssignment}>
                  {nodesSaving ? t("common.saving") : t("admins.saveNodeAssign")}
                </button>
                {nodesSaved && <span className="text-xs text-emerald-600">{t("admins.nodeAssignSaved")}</span>}
              </div>
              {nodesError && <div className="text-xs text-red-500 mt-1">{nodesError}</div>}
            </div>
          )}

          <div className={form.group_id ? "opacity-40 pointer-events-none" : ""}>
            <label className="block text-sm text-gray-600 mb-2">{t("admins.permissionsLabel")}</label>
            <div className="space-y-3">
              {Object.entries(permGroups).map(([groupKey, group]) => (
                <div key={groupKey}>
                  <div className="text-xs font-medium text-gray-500 mb-1">{group.label}</div>
                  <div className="space-y-1.5">
                    {Object.entries(group.perms || {}).map(([key, label]) => (
                      <label key={key} className="flex items-center gap-2 text-sm text-gray-600">
                        <input type="checkbox" checked={form.permissions.includes(key)} onChange={() => togglePerm(key)} />
                        {label}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("admins.billingModeLabel")}</label>
            <select className="input" value={form.billing_mode} onChange={(e) => set("billing_mode", e.target.value)}>
              <option value="flat">{t("admins.billingFlat")}</option>
              <option value="usage">{t("admins.billingUsage")}</option>
            </select>
            <div className="text-xs text-gray-400 mt-1">
              {t("admins.billingHint")}
            </div>
          </div>

          {!editingId && form.billing_mode === "flat" && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("admins.fieldInitialBalance")}</label>
              <input
                type="number"
                className="input"
                placeholder={t("admins.initialBalancePlaceholder")}
                value={form.initial_balance}
                onChange={(e) => set("initial_balance", e.target.value)}
              />
              <div className="text-xs text-gray-400 mt-1">
                {t("admins.initialBalanceHint")}
              </div>
            </div>
          )}

          {!editingId && form.billing_mode === "usage" && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("admins.fieldInitialVolume")}</label>
              <input
                type="number"
                className="input"
                placeholder={t("admins.initialVolumePlaceholder")}
                value={form.initial_volume_gb}
                onChange={(e) => set("initial_volume_gb", e.target.value)}
              />
              <div className="text-xs text-gray-400 mt-1">
                {t("admins.initialVolumeHint")}
              </div>
            </div>
          )}

          {editingId && form.billing_mode === "flat" && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("admins.currentBalance")}</label>
              <div className="flex items-center justify-between bg-gray-50 rounded-xl px-3 py-2.5">
                <span className="flex items-center gap-1.5 font-medium text-gray-700" dir="ltr">
                  <Wallet size={14} className="text-gray-400" /> {formatToman(form.balance)} {t("admins.tomanUnit")}
                </span>
                <button type="button" className="text-xs text-brand-600 flex items-center gap-1" onClick={toggleLogs}>
                  <History size={13} /> {showLogs ? t("admins.hideHistory") : t("admins.balanceHistory")}
                </button>
              </div>

              <div className="flex gap-2 mt-2">
                <input
                  type="number"
                  className="input flex-1"
                  placeholder={t("admins.amountPlaceholder")}
                  value={topupAmount}
                  onChange={(e) => setTopupAmount(e.target.value)}
                />
                <input
                  className="input flex-1"
                  placeholder={t("admins.notePlaceholder")}
                  value={topupNote}
                  onChange={(e) => setTopupNote(e.target.value)}
                />
                <button type="button" className="btn-secondary shrink-0" disabled={topupSaving} onClick={doTopup}>
                  {topupSaving ? "..." : t("admins.submit")}
                </button>
              </div>
              {topupError && <div className="text-xs text-red-500 mt-1">{topupError}</div>}
              <div className="text-xs text-gray-400 mt-1">
                {t("admins.balanceHint")}
              </div>

              {showLogs && (
                <div className="mt-2 border border-gray-100 rounded-xl overflow-hidden">
                  {logsLoading ? (
                    <div className="text-xs text-gray-400 text-center py-4">{t("common.loading")}</div>
                  ) : balanceLogs.length === 0 ? (
                    <div className="text-xs text-gray-400 text-center py-4">{t("admins.noChangesYet")}</div>
                  ) : (
                    <div className="max-h-56 overflow-y-auto divide-y divide-gray-50">
                      {balanceLogs.map((l) => (
                        <div key={l.id} className="flex items-center justify-between px-3 py-2 text-xs">
                          <div className="flex items-center gap-1.5">
                            {l.amount > 0 ? (
                              <TrendingUp size={13} className="text-emerald-500" />
                            ) : (
                              <TrendingDown size={13} className="text-red-500" />
                            )}
                            <span className={l.amount > 0 ? "text-emerald-600 font-medium" : "text-red-500 font-medium"} dir="ltr">
                              {l.amount > 0 ? "+" : ""}
                              {formatToman(l.amount)}
                            </span>
                            {l.note && <span className="text-gray-400">· {l.note}</span>}
                          </div>
                          <div className="text-gray-400 text-left" dir="ltr">
                            <div>{t("admins.remaining", { value: formatToman(l.balance_after) })}</div>
                            <div>{l.created_by_username || "—"}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {editingId && form.billing_mode === "usage" && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("admins.currentVolumeCap")}</label>
              <div className="flex items-center justify-between bg-gray-50 rounded-xl px-3 py-2.5">
                <span className="flex items-center gap-1.5 font-medium text-gray-700" dir="ltr">
                  <Database size={14} className="text-violet-400" /> {formatGb(form.volume_balance_gb)} GB
                </span>
                <button type="button" className="text-xs text-brand-600 flex items-center gap-1" onClick={toggleVolumeLogs}>
                  <History size={13} /> {showVolumeLogs ? t("admins.hideHistory") : t("admins.volumeHistory")}
                </button>
              </div>

              <div className="flex gap-2 mt-2">
                <input
                  type="number"
                  className="input flex-1"
                  placeholder={t("admins.volumeAmountPlaceholder")}
                  value={topupVolumeAmount}
                  onChange={(e) => setTopupVolumeAmount(e.target.value)}
                />
                <input
                  className="input flex-1"
                  placeholder={t("admins.notePlaceholder")}
                  value={topupVolumeNote}
                  onChange={(e) => setTopupVolumeNote(e.target.value)}
                />
                <button type="button" className="btn-secondary shrink-0" disabled={topupVolumeSaving} onClick={doVolumeTopup}>
                  {topupVolumeSaving ? "..." : t("admins.submit")}
                </button>
              </div>
              {topupVolumeError && <div className="text-xs text-red-500 mt-1">{topupVolumeError}</div>}
              <div className="text-xs text-gray-400 mt-1">
                {t("admins.volumeHint")}
              </div>

              {showVolumeLogs && (
                <div className="mt-2 border border-gray-100 rounded-xl overflow-hidden">
                  {volumeLogsLoading ? (
                    <div className="text-xs text-gray-400 text-center py-4">{t("common.loading")}</div>
                  ) : volumeLogs.length === 0 ? (
                    <div className="text-xs text-gray-400 text-center py-4">{t("admins.noChangesYet")}</div>
                  ) : (
                    <div className="max-h-56 overflow-y-auto divide-y divide-gray-50">
                      {volumeLogs.map((l) => (
                        <div key={l.id} className="flex items-center justify-between px-3 py-2 text-xs">
                          <div className="flex items-center gap-1.5">
                            {l.amount_gb > 0 ? (
                              <TrendingUp size={13} className="text-emerald-500" />
                            ) : (
                              <TrendingDown size={13} className="text-red-500" />
                            )}
                            <span className={l.amount_gb > 0 ? "text-emerald-600 font-medium" : "text-red-500 font-medium"} dir="ltr">
                              {l.amount_gb > 0 ? "+" : ""}
                              {formatGb(l.amount_gb)} GB
                            </span>
                            {l.note && <span className="text-gray-400">· {l.note}</span>}
                          </div>
                          <div className="text-gray-400 text-left" dir="ltr">
                            <div>{t("admins.remainingGb", { value: formatGb(l.balance_after_gb) })}</div>
                            <div>{l.created_by_username || "—"}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("admins.fieldTelegramId")}</label>
            <input
              type="number"
              className="input"
              dir="ltr"
              placeholder={t("admins.telegramIdPlaceholder")}
              value={form.telegram_id}
              onChange={(e) => set("telegram_id", e.target.value)}
            />
            <div className="text-xs text-gray-400 mt-1">
              {t("admins.telegramIdHint")}
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("admins.fieldLoginSlug")}</label>
            <div className="flex items-center gap-2" dir="ltr">
              <span className="text-gray-400 text-sm">/a/</span>
              <input
                className="input"
                placeholder={t("admins.loginSlugPlaceholder")}
                value={form.login_slug}
                onChange={(e) => set("login_slug", e.target.value.replace(/[^a-zA-Z0-9_-]/g, ""))}
              />
            </div>
            <div className="text-xs text-gray-400 mt-1">
              {t("admins.loginSlugHint")}
            </div>
          </div>

          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              {t("common.cancel")}
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? t("common.saving") : t("admins.saveAdmin")}
            </button>
          </div>
        </form>
      </Modal>

      <Modal open={groupOpen} onClose={() => setGroupOpen(false)} title={editingGroupId ? t("admins.editGroupModal") : t("admins.newGroupModal")} width="max-w-md">
        <form onSubmit={submitGroup} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("admins.fieldGroupName")}</label>
            <input
              className="input"
              required
              value={groupForm.name}
              onChange={(e) => setGroupForm((f) => ({ ...f, name: e.target.value }))}
              placeholder={t("admins.groupNamePlaceholder")}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-2">{t("admins.permissionsLabel")}</label>
            <div className="space-y-3">
              {Object.entries(permGroups).map(([groupKey, group]) => (
                <div key={groupKey}>
                  <div className="text-xs font-medium text-gray-500 mb-1">{group.label}</div>
                  <div className="space-y-1.5">
                    {Object.entries(group.perms || {}).map(([key, label]) => (
                      <label key={key} className="flex items-center gap-2 text-sm text-gray-600">
                        <input type="checkbox" checked={groupForm.permissions.includes(key)} onChange={() => toggleGroupPerm(key)} />
                        {label}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
          {groupError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{groupError}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setGroupOpen(false)}>
              {t("common.cancel")}
            </button>
            <button type="submit" disabled={groupSaving} className="btn-primary">
              {groupSaving ? t("common.saving") : t("admins.saveGroup")}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
