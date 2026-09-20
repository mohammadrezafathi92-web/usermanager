import React, { useEffect, useMemo, useState } from "react";
import { Plus, Pencil, Trash2, Power, Package as PackageIcon, Server, Paperclip, Download, Check, X, Tag, Layers, GripVertical, Gift, Search } from "lucide-react";
import Layout from "../components/Layout.jsx";
import MoneyInput from "../components/MoneyInput.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import { useConfirm } from "../components/ConfirmDialog.jsx";
import { useToast } from "../components/Toast.jsx";
import {
  fetchPackageGroups,
  createPackageGroup,
  updatePackageGroup,
  deletePackageGroup,
  fetchPackages,
  createPackage,
  updatePackage,
  deletePackage,
  fetchNodes,
  uploadPackageFile,
  deletePackageFile,
  setMyPackagePrice,
} from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";
import { useAuth } from "../context/AuthContext.jsx";
import { formatToman as formatTomanUtil, errorText } from "../utils.js";

const emptyForm = {
  name: "",
  quota_gb: 20,
  duration_days: 30,
  price: 0,
  cooperation_price: "",
  description: "",
  enabled: true,
  bot_enabled: true,
  miniapp_enabled: true,
  is_trial: false,
  trial_daily_cap: "",
  group_id: "",
  seller_visible: true,
  one_time_per_user: false,
  sort_order: 0,
  max_concurrent_sessions: "",
  speed_limit_mbps: "",
  custom_message: "",
  ovpn_templates: [],
  connections: [],
};

function formatFileSize(bytes) {
  if (!bytes) return "0 KB";
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(0)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

// No protocol until a server is chosen - there is no correct default when
// the answer depends entirely on which server it is.
const emptyConn = { node_id: "", protocol: "", flow: "" };

const PROTOCOL_LABELS = { wireguard: "WireGuard", openvpn: "OpenVPN", l2tp: "L2TP", ikev2: "IKEv2", sstp: "SSTP", pptp: "PPTP ⚠️", xray: "V2Ray/Xray" };

// Which protocols a server can actually carry. Same split the bot already
// applies (telegram_bot/keyboards.py's protocols_kb) - a MikroTik cannot
// serve Xray and an Xray node serves nothing else, so offering the full
// list here only ever produced a package whose service fails at
// provisioning time, long after anyone would connect the two.
// PPTP last on purpose: it is offered for old devices that speak nothing
// else, and putting it beside the others invites picking it by accident.
// Its label carries a warning for the same reason - see backend
// models.ConnectionType.pptp.
const MIKROTIK_PROTOCOLS = ["wireguard", "openvpn", "l2tp", "ikev2", "sstp", "pptp"];
const XRAY_PROTOCOLS = ["xray"];
const protocolsForType = (type) => (type === "xray" ? XRAY_PROTOCOLS : MIKROTIK_PROTOCOLS);

export default function Packages() {
  const { t, language } = useLanguage();
  // Hardcoded "fa-IR" before regardless of the active language (found
  // during the 2026-09 full-codebase audit) - now matches every other
  // number/date on this page.
  const formatToman = (n) => formatTomanUtil(n, language);
  const { role, wallet } = useAuth();
  const confirm = useConfirm();
  const toast = useToast();
  const isSeller = role === "seller";
  const [items, setItems] = useState([]);
  const [searchInput, setSearchInput] = useState("");
  const filteredItems = useMemo(() => {
    const q = searchInput.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (p) => (p.name || "").toLowerCase().includes(q) || (p.description || "").toLowerCase().includes(q)
    );
  }, [items, searchInput]);
  const [nodes, setNodes] = useState([]);
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [editingFiles, setEditingFiles] = useState([]);
  const [uploading, setUploading] = useState(false);

  // ---------- Seller's own resale price override (see backend
  // models.PackageSellerPrice) - a Seller can never create/edit/delete a
  // Package itself (still entirely the parent Admin's), only set what
  // price THEIR OWN bot shows/charges for it instead of the base price.
  const [editingPriceId, setEditingPriceId] = useState(null);
  const [priceDraft, setPriceDraft] = useState("");
  const [priceSaving, setPriceSaving] = useState(false);
  const [priceError, setPriceError] = useState("");

  const [groups, setGroups] = useState([]);
  const [groupsOpen, setGroupsOpen] = useState(false);

  const load = () => fetchPackages().then((res) => setItems(res.data));
  // Its own loader, called again after the groups dialog closes: a group
  // created in there has to appear in the package form's dropdown without
  // a page reload, which is the first thing anyone does after making one.
  const loadGroups = () => fetchPackageGroups().then((res) => setGroups(res.data)).catch(() => setGroups([]));
  useEffect(() => {
    load();
    loadGroups();
    fetchNodes().then((res) => setNodes(res.data));
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  // What this package costs the person filling in the form. The backend
  // refuses a cooperation price below it (routers/packages.py's
  // _check_cooperation_floor); showing it here means they see the number
  // rather than discovering it by being rejected.
  //
  // Declared AFTER `form`, which is the whole point: reading a `const`
  // declared further down throws on every render, and a page that throws
  // during render shows nothing at all - which is exactly how this page
  // stopped opening.
  const perGbRate = Number(wallet?.wholesale_price_per_gb || 0);
  const costFloor =
    perGbRate > 0 && Number(form.quota_gb) > 0 ? Math.round(Number(form.quota_gb) * perGbRate) : null;
  const belowFloor =
    costFloor !== null && form.cooperation_price !== "" && Number(form.cooperation_price) < costFloor;

  // The package's own retail price must never undercut what it actually
  // costs (see routers/packages.py's _effective_package_cost/
  // _check_price_floor) - the real per-GB cost when one is configured,
  // otherwise whatever cooperation_price was typed above.
  const priceFloor =
    costFloor !== null ? costFloor : form.cooperation_price !== "" ? Number(form.cooperation_price) : null;
  const belowPriceFloor = priceFloor !== null && Number(form.price) < priceFloor;

  const nodeName = (id) => nodes.find((n) => n.id === Number(id))?.name || `#${id}`;

  const addConn = () => setForm((f) => ({ ...f, connections: [...f.connections, { ...emptyConn }] }));
  const removeConn = (idx) => setForm((f) => ({ ...f, connections: f.connections.filter((_, i) => i !== idx) }));
  const updateConn = (idx, k, v) =>
    setForm((f) => ({
      ...f,
      connections: f.connections.map((c, i) => {
        if (i !== idx) return c;
        const next = { ...c, [k]: v };
        if (k === "node_id") {
          // Changing the server can invalidate the protocol beside it -
          // picking an Xray node while "L2TP" is selected would otherwise
          // leave a pair that cannot exist, saved without complaint.
          const allowed = protocolsForType(nodes.find((n) => String(n.id) === String(v))?.type);
          if (!allowed.includes(next.protocol)) next.protocol = allowed[0];
          if (next.protocol !== "xray") next.flow = "";
        }
        return next;
      }),
    }));

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
      group_id: pkg.group_id ?? "",
      max_concurrent_sessions: pkg.max_concurrent_sessions ?? "",
      speed_limit_mbps: pkg.speed_limit_mbps ?? "",
      custom_message: pkg.custom_message || "",
      ovpn_templates: (pkg.ovpn_templates || []).map((tpl) => ({ name: tpl.name, content: tpl.content })),
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
      setError(errorText(err, t("packages.uploadError")));
    } finally {
      setUploading(false);
    }
  };

  const onDeleteFile = async (fileId) => {
    if (!editingId) return;
    try {
      await deletePackageFile(editingId, fileId);
      setEditingFiles((files) => files.filter((f) => f.id !== fileId));
    } catch (err) {
      toast.error(errorText(err, t("packages.deleteFileError")));
    }
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      const payload = {
        ...form,
        // "" is the «بدون دسته» option. Sent as null, not dropped: an
        // omitted field leaves the old group in place, which would make
        // "no group" the one choice the form cannot express.
        group_id: form.group_id === "" || form.group_id === null ? null : Number(form.group_id),
        cooperation_price: form.cooperation_price === "" ? null : Number(form.cooperation_price),
        max_concurrent_sessions: form.max_concurrent_sessions === "" ? null : Number(form.max_concurrent_sessions),
        speed_limit_mbps: form.speed_limit_mbps === "" ? null : Number(form.speed_limit_mbps),
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
      setError(errorText(err, t("packages.saveError")));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (id) => {
    if (!(await confirm({ message: t("packages.deleteConfirm"), danger: true }))) return;
    try {
      await deletePackage(id);
      load();
    } catch (err) {
      // No try/catch here before meant ANY failure - wrong/expired confirm
      // password, a package still referenced somewhere, a network hiccup -
      // surfaced as nothing at all: the row just stayed put with no
      // explanation, identical to the button not working (same bug class
      // already fixed once for Users.jsx's onDelete).
      toast.error(errorText(err, t("packages.deleteError")));
    }
  };

  const onToggle = async (pkg) => {
    try {
      await updatePackage(pkg.id, { enabled: !pkg.enabled });
      load();
    } catch (err) {
      toast.error(errorText(err, t("packages.toggleError")));
    }
  };

  const startEditPrice = (pkg) => {
    setEditingPriceId(pkg.id);
    setPriceDraft(pkg.my_price != null ? String(pkg.my_price) : String(pkg.price));
    setPriceError("");
  };
  const cancelEditPrice = () => {
    setEditingPriceId(null);
    setPriceDraft("");
    setPriceError("");
  };
  const saveMyPrice = async (pkgId) => {
    setPriceSaving(true);
    setPriceError("");
    try {
      await setMyPackagePrice(pkgId, priceDraft === "" ? null : Number(priceDraft));
      await load();
      cancelEditPrice();
    } catch (err) {
      // The backend refuses a resale price below the package's cooperation
      // cost (routers/packages.py's _check_price_floor) - without this the
      // editor just silently stayed open with the rejected value still in
      // it and no explanation, identical in spirit to the delete-button
      // bug already fixed once on this same page.
      setPriceError(errorText(err, t("packages.saveError")));
    } finally {
      setPriceSaving(false);
    }
  };
  const clearMyPrice = async (pkgId) => {
    setPriceSaving(true);
    try {
      await setMyPackagePrice(pkgId, null);
      await load();
      cancelEditPrice();
    } finally {
      setPriceSaving(false);
    }
  };

  return (
    <Layout>
      <Topbar title={t("packages.title")} subtitle={t("packages.subtitle")} />

      {!isSeller && (
        <TrialCard
          t={t}
          trial={items.find((p) => p.is_trial) || null}
          nodes={nodes}
          onChanged={load}
        />
      )}
      <div className="flex items-center gap-2 flex-wrap justify-between mb-4">
        <div className="relative">
          <Search className="absolute end-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" size={16} />
          <input
            className="input pe-9 !w-full sm:!w-56"
            placeholder={t("packages.search")}
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </div>
        {!isSeller && (
          <div className="flex gap-2">
            <button className="btn-outline" onClick={() => setGroupsOpen(true)}>
              <Layers size={16} /> {t("packages.manageGroups")}
            </button>
            <button className="btn-primary" onClick={openCreate}>
              <Plus size={16} /> {t("packages.newPackage")}
            </button>
          </div>
        )}
      </div>
      {isSeller && (
        <div className="text-xs text-gray-400 mb-4">{t("packages.sellerPriceHint")}</div>
      )}

      <div className="card !p-0">
        {/* دسکتاپ: جدول - از md به بالا نمایش داده می‌شود */}
        <div className="hidden md:block table-wrap !mx-0">
          <table className="w-full text-sm min-w-[48rem]">
            <thead>
              <tr>
                <th>{t("packages.colName")}</th>
                <th>{t("packages.colQuota")}</th>
                <th>{t("packages.colDuration")}</th>
                <th>{t("packages.colPrice")}</th>
                <th>{t("packages.colStatus")}</th>
                <th>{t("packages.colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {filteredItems.map((p) => (
                <tr key={p.id}>
                  <td>
                    <div className="font-medium text-gray-800">{p.name}</div>
                    {p.description && <div className="text-xs text-gray-400">{p.description}</div>}
                    {p.connections?.length > 0 && (
                      <div className="text-xs text-brand-600 flex items-center gap-1 mt-1">
                        <Server size={12} /> {t("packages.bundledServices", { count: p.connections.length })}
                        {p.max_concurrent_sessions ? t("packages.maxConcurrent", { count: p.max_concurrent_sessions }) : ""}
                      </div>
                    )}
                    {p.speed_limit_mbps ? (
                      <div className="text-xs text-amber-600 dark:text-amber-400 mt-1">{t("packages.speedLimitBadge", { mbps: p.speed_limit_mbps })}</div>
                    ) : null}
                  </td>
                  <td className="text-gray-600">{p.quota_gb ? `${p.quota_gb} GB` : t("packages.unlimited")}</td>
                  <td className="text-gray-600">{p.duration_days ? t("packages.days", { count: p.duration_days }) : t("packages.noExpiry")}</td>
                  <td className="text-gray-600" dir="ltr">
                    {!isSeller && (
                      <>
                        {formatToman(p.price)}
                        {p.cooperation_price != null && (
                          <div className="text-xs text-gray-400">{t("packages.cooperationLabel", { price: formatToman(p.cooperation_price) })}</div>
                        )}
                      </>
                    )}
                    {isSeller && editingPriceId !== p.id && (
                      <div className="flex items-center gap-2">
                        <div>
                          <div className={p.my_price != null ? "text-gray-400 line-through text-xs" : ""}>{formatToman(p.price)}</div>
                          {p.my_price != null && (
                            <div className="text-brand-600 font-medium flex items-center gap-1">
                              <Tag size={12} /> {formatToman(p.my_price)}
                            </div>
                          )}
                        </div>
                        <button title={t("packages.editMyPrice")} onClick={() => startEditPrice(p)} className="text-gray-400 hover:text-brand-600">
                          <Pencil size={14} />
                        </button>
                      </div>
                    )}
                    {isSeller && editingPriceId === p.id && (
                      <div dir="ltr">
                        <div className="flex items-center gap-1">
                          <MoneyInput
                            autoFocus
                            small
                            className="w-28"
                            value={priceDraft}
                            onChange={(v) => { setPriceDraft(v); setPriceError(""); }}
                          />
                          <button disabled={priceSaving} title={t("common.save")} onClick={() => saveMyPrice(p.id)} className="text-emerald-500 hover:text-emerald-600">
                            <Check size={16} />
                          </button>
                          <button disabled={priceSaving} title={t("common.cancel")} onClick={cancelEditPrice} className="text-gray-400 hover:text-gray-600">
                            <X size={16} />
                          </button>
                          {p.my_price != null && (
                            <button disabled={priceSaving} title={t("packages.resetMyPrice")} onClick={() => clearMyPrice(p.id)} className="text-xs text-gray-400 hover:text-red-500 underline">
                              {t("packages.resetMyPrice")}
                            </button>
                          )}
                        </div>
                        {p.cooperation_price != null && priceDraft !== "" && Number(priceDraft) < p.cooperation_price && (
                          <div className="text-xs mt-1 text-red-600 dark:text-red-400 font-medium" dir="rtl">
                            {t("packages.priceBelowCost", { floor: formatToman(p.cooperation_price) })}
                          </div>
                        )}
                        {priceError && <div className="text-xs mt-1 text-red-600 dark:text-red-400" dir="rtl">{priceError}</div>}
                      </div>
                    )}
                  </td>
                  <td>
                    <div className="flex flex-col gap-1 items-start">
                      <span className={p.enabled ? "badge-success" : "badge-neutral"}>
                        {t("packages.webPanel")}: {p.enabled ? t("status.active") : t("status.disabled")}
                      </span>
                      <span className={p.bot_enabled ? "badge-success" : "badge-neutral"}>
                        {t("packages.bot")}: {p.bot_enabled ? t("status.active") : t("status.disabled")}
                      </span>
                      {!isSeller && (
                        <span className={p.seller_visible ? "badge-success" : "badge-neutral"}>
                          {t("packages.sellers")}: {p.seller_visible ? t("status.active") : t("status.disabled")}
                        </span>
                      )}
                      {p.one_time_per_user && (
                        <span className="badge-warn">{t("packages.oneTimePerUser")}</span>
                      )}
                    </div>
                  </td>
                  <td>
                    {!isSeller && (
                      <div className="flex items-center gap-2">
                        <button title={p.enabled ? t("packages.disable") : t("packages.enable")} onClick={() => onToggle(p)} className="text-gray-400 hover:text-brand-600">
                          <Power size={16} />
                        </button>
                        <button title={t("packages.editTitle")} onClick={() => openEdit(p)} className="text-gray-400 hover:text-brand-600">
                          <Pencil size={16} />
                        </button>
                        <button title={t("packages.deleteTitle")} onClick={() => onDelete(p.id)} className="text-gray-400 hover:text-red-600">
                          <Trash2 size={16} />
                        </button>
                      </div>
                    )}
                    {isSeller && <span className="text-gray-300 text-xs">—</span>}
                  </td>
                </tr>
              ))}
              {filteredItems.length === 0 && (
                <tr>
                  <td colSpan={6} className="empty-state">
                    <PackageIcon size={28} className="mx-auto mb-2 text-gray-300" />
                    {items.length === 0 ? t("packages.empty") : t("common.noResults")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* موبایل: کارت - زیر md نمایش داده می‌شود */}
        <div className="md:hidden divide-y divide-gray-100 dark:divide-slate-800">
          {filteredItems.map((p) => (
            <div key={p.id} className="p-4">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="font-medium text-gray-800">{p.name}</div>
                  {p.description && <div className="text-xs text-gray-400">{p.description}</div>}
                  {p.connections?.length > 0 && (
                    <div className="text-xs text-brand-600 flex items-center gap-1 mt-1">
                      <Server size={12} /> {t("packages.bundledServices", { count: p.connections.length })}
                      {p.max_concurrent_sessions ? t("packages.maxConcurrent", { count: p.max_concurrent_sessions }) : ""}
                    </div>
                  )}
                  {p.speed_limit_mbps ? (
                    <div className="text-xs text-amber-600 dark:text-amber-400 mt-1">{t("packages.speedLimitBadge", { mbps: p.speed_limit_mbps })}</div>
                  ) : null}
                </div>
                {!isSeller ? (
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <button title={p.enabled ? t("packages.disable") : t("packages.enable")} onClick={() => onToggle(p)} className="text-gray-400 hover:text-brand-600">
                      <Power size={16} />
                    </button>
                    <button title={t("packages.editTitle")} onClick={() => openEdit(p)} className="text-gray-400 hover:text-brand-600">
                      <Pencil size={16} />
                    </button>
                    <button title={t("packages.deleteTitle")} onClick={() => onDelete(p.id)} className="text-gray-400 hover:text-red-600">
                      <Trash2 size={16} />
                    </button>
                  </div>
                ) : (
                  editingPriceId !== p.id && (
                    <button title={t("packages.editMyPrice")} onClick={() => startEditPrice(p)} className="text-gray-400 hover:text-brand-600 flex-shrink-0">
                      <Pencil size={14} />
                    </button>
                  )
                )}
              </div>

              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2 text-xs text-gray-600">
                <span>{p.quota_gb ? `${p.quota_gb} GB` : t("packages.unlimited")}</span>
                <span>{p.duration_days ? t("packages.days", { count: p.duration_days }) : t("packages.noExpiry")}</span>
                {!isSeller && (
                  <span dir="ltr">
                    {formatToman(p.price)}
                    {p.cooperation_price != null && (
                      <span className="text-gray-400"> ({t("packages.cooperationLabel", { price: formatToman(p.cooperation_price) })})</span>
                    )}
                  </span>
                )}
              </div>

              {isSeller && (
                <div className="mt-1" dir="ltr">
                  {editingPriceId !== p.id ? (
                    <div className="flex items-center gap-1 text-xs">
                      <span className={p.my_price != null ? "text-gray-400 line-through" : "text-gray-600"}>{formatToman(p.price)}</span>
                      {p.my_price != null && (
                        <span className="text-brand-600 font-medium flex items-center gap-1">
                          <Tag size={12} /> {formatToman(p.my_price)}
                        </span>
                      )}
                    </div>
                  ) : (
                    <div>
                      <div className="flex items-center gap-1">
                        <MoneyInput
                          autoFocus
                          small
                          className="w-28"
                          value={priceDraft}
                          onChange={(v) => { setPriceDraft(v); setPriceError(""); }}
                        />
                        <button disabled={priceSaving} title={t("common.save")} onClick={() => saveMyPrice(p.id)} className="text-emerald-500 hover:text-emerald-600">
                          <Check size={16} />
                        </button>
                        <button disabled={priceSaving} title={t("common.cancel")} onClick={cancelEditPrice} className="text-gray-400 hover:text-gray-600">
                          <X size={16} />
                        </button>
                        {p.my_price != null && (
                          <button disabled={priceSaving} title={t("packages.resetMyPrice")} onClick={() => clearMyPrice(p.id)} className="text-xs text-gray-400 hover:text-red-500 underline">
                            {t("packages.resetMyPrice")}
                          </button>
                        )}
                      </div>
                      {p.cooperation_price != null && priceDraft !== "" && Number(priceDraft) < p.cooperation_price && (
                        <div className="text-xs mt-1 text-red-600 dark:text-red-400 font-medium" dir="rtl">
                          {t("packages.priceBelowCost", { floor: formatToman(p.cooperation_price) })}
                        </div>
                      )}
                      {priceError && <div className="text-xs mt-1 text-red-600 dark:text-red-400" dir="rtl">{priceError}</div>}
                    </div>
                  )}
                </div>
              )}

              <div className="flex flex-wrap gap-1 items-center mt-2">
                <span className={p.enabled ? "badge-success" : "badge-neutral"}>
                  {t("packages.webPanel")}: {p.enabled ? t("status.active") : t("status.disabled")}
                </span>
                <span className={p.bot_enabled ? "badge-success" : "badge-neutral"}>
                  {t("packages.bot")}: {p.bot_enabled ? t("status.active") : t("status.disabled")}
                </span>
                {!isSeller && (
                  <span className={p.seller_visible ? "badge-success" : "badge-neutral"}>
                    {t("packages.sellers")}: {p.seller_visible ? t("status.active") : t("status.disabled")}
                  </span>
                )}
                {p.one_time_per_user && (
                  <span className="badge-warn">{t("packages.oneTimePerUser")}</span>
                )}
              </div>
            </div>
          ))}
          {filteredItems.length === 0 && (
            <div className="empty-state">
              <PackageIcon size={28} className="mx-auto mb-2 text-gray-300" />
              {items.length === 0 ? t("packages.empty") : t("common.noResults")}
            </div>
          )}
        </div>
      </div>

      <Modal open={open} onClose={() => setOpen(false)} title={editingId ? t("packages.editModal") : t("packages.newModal")} width="max-w-2xl">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldName")}</label>
            <input className="input" required placeholder={t("packages.fieldNamePlaceholder")} value={form.name} onChange={(e) => set("name", e.target.value)} />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldQuota")}</label>
              <input type="number" step="0.1" min="0" className="input" value={form.quota_gb} onChange={(e) => set("quota_gb", Number(e.target.value))} />
              <div className="hint">{t("packages.quotaHint")}</div>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldDuration")}</label>
              <input type="number" min="0" className="input" value={form.duration_days ?? ""} onChange={(e) => set("duration_days", e.target.value ? Number(e.target.value) : null)} />
              <div className="hint">{t("packages.durationHint")}</div>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldPrice")}</label>
              <MoneyInput value={form.price} onChange={(v) => set("price", v === "" ? 0 : Number(v))} />
              {belowPriceFloor && (
                <div className="text-xs mt-1 text-red-600 dark:text-red-400 font-medium">
                  {t("packages.priceBelowCost", { floor: formatToman(priceFloor) })}
                </div>
              )}
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldCooperationPrice")}</label>
            <MoneyInput
              placeholder={t("packages.cooperationPricePlaceholder")}
              value={form.cooperation_price}
              onChange={(v) => set("cooperation_price", v)}
            />
            <div className="hint">
              {t("packages.cooperationHint")}
            </div>
            {costFloor !== null && (
              <div className={`text-xs mt-1 ${belowFloor ? "text-red-600 dark:text-red-400 font-medium" : "text-gray-400"}`}>
                {belowFloor
                  ? t("packages.cooperationBelowCost", { floor: formatToman(costFloor) })
                  : t("packages.cooperationYourCost", { cost: formatToman(costFloor) })}
              </div>
            )}
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldDescription")}</label>
            <textarea className="input" rows={2} value={form.description || ""} onChange={(e) => set("description", e.target.value)} />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldOrder")}</label>
              <input type="number" className="input" value={form.sort_order} onChange={(e) => set("sort_order", Number(e.target.value))} />
            </div>
            {/* Which card this plan appears on in the Mini App. */}
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldGroup")}</label>
              <select className="input" value={form.group_id} onChange={(e) => set("group_id", e.target.value)}>
                <option value="">{t("packages.noGroup")}</option>
                {groups.map((g) => (
                  <option key={g.id} value={g.id}>{g.name}</option>
                ))}
              </select>
              <div className="hint">{t("packages.fieldGroupHint")}</div>
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
              {t("packages.showInPanel")}
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={form.bot_enabled} onChange={(e) => set("bot_enabled", e.target.checked)} />
              {t("packages.showInBot")}
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={form.miniapp_enabled} onChange={(e) => set("miniapp_enabled", e.target.checked)} />
              {t("packages.showInMiniApp")}
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={form.seller_visible} onChange={(e) => set("seller_visible", e.target.checked)} />
              {t("packages.showToSellers")}
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={form.one_time_per_user} onChange={(e) => set("one_time_per_user", e.target.checked)} />
              {t("packages.oneTimePerUser")}
            </label>
          </div>
          <div className="text-xs text-gray-400 -mt-2">{t("packages.oneTimePerUserHint")}</div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldMaxConcurrent")}</label>
            <input
              type="number"
              min="0"
              className="input"
              placeholder={t("packages.maxConcurrentPlaceholder")}
              value={form.max_concurrent_sessions}
              onChange={(e) => set("max_concurrent_sessions", e.target.value)}
            />
            <div className="hint">
              {t("packages.maxConcurrentHint")}
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldSpeedLimit")}</label>
            <input
              type="number"
              min="0"
              className="input"
              placeholder={t("packages.speedLimitPlaceholder")}
              value={form.speed_limit_mbps}
              onChange={(e) => set("speed_limit_mbps", e.target.value)}
            />
            <div className="hint">{t("packages.speedLimitHint")}</div>
          </div>

          <div className="border-t border-gray-100 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-medium text-gray-700">
                {t("packages.servicesHeading")}
              </div>
              <button type="button" className="btn-secondary" onClick={addConn}>
                <Plus size={14} /> {t("packages.addService")}
              </button>
            </div>
            {form.connections.length === 0 && (
              <div className="text-xs text-gray-400">{t("packages.noServices")}</div>
            )}
            {form.connections.map((c, idx) => (
              <div key={idx} className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-2 items-center">
                <select className="input col-span-2" value={c.node_id} onChange={(e) => updateConn(idx, "node_id", e.target.value)}>
                  <option value="">{t("packages.selectServer")}</option>
                  {nodes.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.name}
                    </option>
                  ))}
                </select>
                {/* Only what the chosen server can actually carry. Until a
                    server is chosen there is no answer, so the field says
                    so instead of offering six protocols and letting five of
                    them be wrong. */}
                <select
                  className="input"
                  value={c.protocol}
                  disabled={!c.node_id}
                  onChange={(e) => updateConn(idx, "protocol", e.target.value)}
                >
                  {!c.node_id ? (
                    <option value="">{t("packages.selectServerFirst")}</option>
                  ) : (
                    protocolsForType(nodes.find((n) => String(n.id) === String(c.node_id))?.type).map((v) => (
                      <option key={v} value={v}>
                        {PROTOCOL_LABELS[v]}
                      </option>
                    ))
                  )}
                </select>
                <div className="flex items-center gap-1">
                  {c.protocol === "xray" ? (
                    <input
                      className="input"
                      placeholder={t("packages.flowPlaceholder")}
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
              {t("packages.customMessageHeading")}
            </label>
            <textarea
              className="input"
              rows={3}
              placeholder={t("packages.customMessagePlaceholder")}
              value={form.custom_message}
              onChange={(e) => set("custom_message", e.target.value)}
            />
          </div>

          {/* Admin's ready-made .ovpn files used VERBATIM for this
              package's OpenVPN services - the panel injects only each
              customer's own credentials (see services/link_builder.py's
              render_ovpn_template) and never rewrites remote/port/certs.
              A package can carry SEVERAL (one per server/port variant);
              the customer receives all of them. */}
          <div className="border-t border-gray-100 pt-3">
            <div className="flex items-center justify-between mb-2">
              <label className="block text-sm text-gray-600">
                {t("packages.ovpnTemplateHeading")}
              </label>
              <label className="btn-secondary cursor-pointer">
                <Paperclip size={14} /> {t("packages.ovpnTemplateUpload")}
                <input
                  type="file"
                  accept=".ovpn,.conf,text/plain"
                  multiple
                  className="hidden"
                  onChange={async (e) => {
                    const files = Array.from(e.target.files || []);
                    if (!files.length) return;
                    const added = await Promise.all(
                      files.map(async (f) => ({ name: f.name, content: await f.text() }))
                    );
                    set("ovpn_templates", [...(form.ovpn_templates || []), ...added]);
                    e.target.value = "";
                  }}
                />
              </label>
            </div>

            {(form.ovpn_templates || []).length === 0 ? (
              <div className="text-xs text-gray-400">{t("packages.ovpnTemplateEmpty")}</div>
            ) : (
              <div className="space-y-2">
                {(form.ovpn_templates || []).map((tpl, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <input
                      className="input flex-1 text-xs"
                      value={tpl.name}
                      placeholder={t("packages.ovpnTemplateNamePlaceholder")}
                      onChange={(e) => {
                        const next = [...form.ovpn_templates];
                        next[i] = { ...next[i], name: e.target.value };
                        set("ovpn_templates", next);
                      }}
                    />
                    <span className="text-[11px] text-gray-400 shrink-0" dir="ltr">
                      {Math.round((tpl.content || "").length / 1024)} KB
                    </span>
                    <button
                      type="button"
                      className="text-red-500 hover:text-red-700 shrink-0"
                      onClick={() => set("ovpn_templates", form.ovpn_templates.filter((_, j) => j !== i))}
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="text-xs text-gray-400 mt-2">{t("packages.ovpnTemplateHint")}</div>
          </div>

          <div className="border-t border-gray-100 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-medium text-gray-700">
                {t("packages.filesHeading")}
              </div>
              {editingId ? (
                <label className="btn-secondary cursor-pointer">
                  <Paperclip size={14} /> {uploading ? t("packages.uploading") : t("packages.addFile")}
                  <input type="file" className="hidden" onChange={onUploadFile} disabled={uploading} />
                </label>
              ) : null}
            </div>
            {!editingId && (
              <div className="text-xs text-gray-400">{t("packages.saveFirst")}</div>
            )}
            {editingId && editingFiles.length === 0 && (
              <div className="text-xs text-gray-400">{t("packages.noFiles")}</div>
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

          {error && <div className="text-sm text-red-500 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              {t("common.cancel")}
            </button>
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? t("common.saving") : t("packages.savePackage")}
            </button>
          </div>
        </form>
      </Modal>

      <GroupsManager
        open={groupsOpen}
        onClose={() => setGroupsOpen(false)}
        groups={groups}
        onChanged={async () => {
          await loadGroups();
          // The packages list carries group_id, and deleting a group
          // changes it underneath - reloading both keeps the dropdown and
          // the rows telling the same story.
          load();
        }}
        t={t}
      />
    </Layout>
  );
}

/**
 * The shelves of the Mini App's shop (see backend models.PackageGroup).
 *
 * A dialog rather than a page of its own: a group is a name and a switch,
 * and it only ever means anything next to the packages it holds, which are
 * on the page behind this. Its own route would put the two things that have
 * to agree in two different places.
 *
 * Deleting one is the only destructive action here, and it is not very
 * destructive: the backend sets its packages' group_id to NULL, so they
 * move to «سایر پلن‌ها» and stay on sale. The confirmation says so, because
 * "delete group" reads like it takes the packages with it.
 */
function GroupsManager({ open, onClose, groups, onChanged, t }) {
  const confirm = useConfirm();
  const [draft, setDraft] = useState({ name: "", description: "", sort_order: 0 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const run = async (action) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await onChanged();
    } catch (err) {
      setError(errorText(err, t("packages.saveError")));
    } finally {
      setBusy(false);
    }
  };

  const add = () => {
    if (!draft.name.trim()) return;
    run(async () => {
      await createPackageGroup({ ...draft, name: draft.name.trim() });
      setDraft({ name: "", description: "", sort_order: 0 });
    });
  };

  const remove = async (group) => {
    const count = group.package_count || 0;
    const warning = count
      ? t("packages.groupDeleteWithPackages").replace("{n}", count)
      : t("packages.groupDeleteConfirm");
    if (!(await confirm({ message: warning, danger: true }))) return;
    run(() => deletePackageGroup(group.id));
  };

  return (
    <Modal open={open} onClose={onClose} title={t("packages.manageGroups")} width="max-w-xl">
      <div className="space-y-3">
        <div className="text-xs text-gray-400">{t("packages.groupsHint")}</div>

        {groups.length === 0 && (
          <div className="text-sm text-gray-400 text-center py-6">{t("packages.noGroupsYet")}</div>
        )}

        {groups.map((group) => (
          <div key={group.id} className="border border-gray-200 rounded-xl p-3">
            <div className="flex items-center gap-2">
              <GripVertical size={15} className="text-gray-300 shrink-0" />
              <input
                className="input input-sm flex-1"
                defaultValue={group.name}
                disabled={busy}
                // Saved on blur rather than with a per-row save button: the
                // whole dialog is a list of one-field edits, and a button
                // beside each of them is more chrome than content.
                onBlur={(e) => {
                  const name = e.target.value.trim();
                  if (name && name !== group.name) run(() => updatePackageGroup(group.id, { name }));
                }}
              />
              <input
                type="number"
                className="input input-sm w-20"
                defaultValue={group.sort_order}
                disabled={busy}
                title={t("packages.fieldOrder")}
                onBlur={(e) => {
                  const sort_order = Number(e.target.value);
                  if (sort_order !== group.sort_order) run(() => updatePackageGroup(group.id, { sort_order }));
                }}
              />
              <button
                type="button"
                disabled={busy}
                title={group.enabled ? t("packages.disable") : t("packages.enable")}
                className={group.enabled ? "text-emerald-600" : "text-gray-300"}
                onClick={() => run(() => updatePackageGroup(group.id, { enabled: !group.enabled }))}
              >
                <Power size={16} />
              </button>
              <button type="button" disabled={busy} className="text-gray-400 hover:text-red-600" onClick={() => remove(group)}>
                <Trash2 size={16} />
              </button>
            </div>
            <input
              className="input input-sm mt-2"
              defaultValue={group.description || ""}
              placeholder={t("packages.groupDescriptionPlaceholder")}
              disabled={busy}
              onBlur={(e) => {
                const description = e.target.value;
                if (description !== (group.description || "")) run(() => updatePackageGroup(group.id, { description }));
              }}
            />
            <div className="text-xs text-gray-400 mt-2">
              {t("packages.groupPackageCount").replace("{n}", group.package_count || 0)}
            </div>
          </div>
        ))}

        <div className="border-t border-gray-100 pt-3 flex gap-2">
          <input
            className="input input-sm flex-1"
            placeholder={t("packages.newGroupPlaceholder")}
            value={draft.name}
            disabled={busy}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
          />
          <button type="button" className="btn-primary btn-sm" disabled={busy || !draft.name.trim()} onClick={add}>
            <Plus size={15} /> {t("packages.addGroup")}
          </button>
        </div>

        {error && <div className="text-xs text-red-600 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2">{error}</div>}
      </div>
    </Modal>
  );
}


/**
 * The free sample, in one button.
 *
 * A trial IS a package (see backend models.Package.is_trial), so this card
 * is a shortcut rather than a separate system: press it once and a package
 * appears with the defaults that make a trial a trial - 200 MB, one day,
 * price zero, one purchase per customer, a daily ceiling. Press it again
 * and it goes on or off.
 *
 * The shortcut exists because the alternative is a form with twenty fields
 * where nineteen of the answers are always the same, and the twentieth -
 * which servers - is the only one worth asking. Everything it creates is
 * an ordinary package underneath, editable in the ordinary way, so nothing
 * here is a dead end.
 */
function TrialCard({ t, trial, nodes, onChanged }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [picking, setPicking] = useState(false);
  // Pairs, not node ids. A server does not carry "one protocol" - a
  // MikroTik can serve five and an Xray node one, and which of them the
  // trial should hand out is the single question about a trial that has
  // no obvious answer. Picking only the server and defaulting the protocol
  // silently decided it, which is how the first version shipped.
  const [chosen, setChosen] = useState([]);
  const has = (nodeId, protocol) =>
    chosen.some((c) => c.node_id === nodeId && c.protocol === protocol);
  const toggleProtocol = (nodeId, protocol) =>
    setChosen((c) =>
      has(nodeId, protocol)
        ? c.filter((x) => !(x.node_id === nodeId && x.protocol === protocol))
        : [...c, { node_id: nodeId, protocol }]
    );

  const run = async (action) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await onChanged();
    } catch (err) {
      setError(errorText(err, t("packages.saveError")));
    } finally {
      setBusy(false);
    }
  };

  const create = () =>
    run(async () => {
      await createPackage({
        ...emptyForm,
        // emptyForm carries "" for the optional numeric fields because an
        // empty <input> is "", and the backend's Optional[int] refuses it -
        // a 422 whose detail is a LIST, which is what actually blanked this
        // page: React throws on an array rendered as a child. Both halves
        // are fixed; this is the half that stops the 422 happening at all.
        group_id: null,
        max_concurrent_sessions: null,
        speed_limit_mbps: null,
        name: t("packages.trialDefaultName"),
        // 200 MB. quota_gb is a float, so a fraction is not a hack here -
        // it is the field doing what it was declared to do.
        quota_gb: 0.2,
        duration_days: 1,
        price: 0,
        cooperation_price: null,
        is_trial: true,
        one_time_per_user: true,
        trial_daily_cap: 20,
        connections: chosen.map(({ node_id, protocol }) => ({ node_id, protocol, flow: "" })),
      });
      setPicking(false);
      setChosen([]);
    });

  const toggle = () =>
    run(() => updatePackage(trial.id, { bot_enabled: !trial.bot_enabled, miniapp_enabled: !trial.bot_enabled }));

  const on = trial && trial.bot_enabled;

  return (
    <div className="card mb-4">
      <div className="flex items-start gap-3">
        <div className="w-11 h-11 rounded-2xl bg-violet-50 text-violet-600 dark:bg-violet-500/10 dark:text-violet-400 flex items-center justify-center shrink-0">
          <Gift size={20} />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="font-bold text-gray-700">{t("packages.trialTitle")}</h3>
          <p className="text-xs text-gray-400 mt-1">{t("packages.trialHint")}</p>
        </div>
        {trial && (
          <button
            type="button"
            disabled={busy}
            onClick={toggle}
            className={on ? "btn-secondary" : "btn-primary"}
          >
            <Power size={15} /> {on ? t("packages.disable") : t("packages.enable")}
          </button>
        )}
      </div>

      {trial ? (
        <div className="text-xs text-gray-500 mt-3 flex flex-wrap gap-x-4 gap-y-1">
          <span className={on ? "text-emerald-600" : "text-gray-400"}>
            {on ? t("status.active") : t("status.disabled")}
          </span>
          <span>{trial.name}</span>
          <span dir="ltr">{trial.quota_gb} GB / {trial.duration_days} d</span>
          {trial.trial_daily_cap ? (
            <span>{t("packages.trialCapLabel").replace("{n}", trial.trial_daily_cap)}</span>
          ) : null}
        </div>
      ) : !picking ? (
        <button type="button" className="btn-primary mt-3" disabled={busy} onClick={() => setPicking(true)}>
          <Plus size={15} /> {t("packages.trialCreate")}
        </button>
      ) : (
        <div className="mt-3">
          {/* The one question worth asking. Everything else about a trial
              has an obvious answer; which servers it runs on does not. */}
          <div className="text-sm text-gray-600 mb-2">{t("packages.trialPickNodes")}</div>
          <div className="space-y-2">
            {nodes.map((n) => (
              <div key={n.id} className="flex flex-wrap items-center gap-2 border border-gray-100 rounded-xl p-2.5">
                <div className="text-xs text-gray-600 min-w-24 flex items-center gap-1.5">
                  <Server size={13} className="text-gray-300" />
                  {n.name}
                </div>
                {/* Only what this server can actually carry - a MikroTik
                    cannot serve Xray and an Xray node serves nothing else.
                    Offering the full list would produce a trial that fails
                    at provisioning time, long after anyone links the two. */}
                {protocolsForType(n.type).map((protocol) => (
                  <button
                    key={protocol}
                    type="button"
                    onClick={() => toggleProtocol(n.id, protocol)}
                    className={`px-2.5 py-1 rounded-lg text-xs border ${
                      has(n.id, protocol)
                        ? "bg-brand-600 text-white border-brand-600"
                        : "border-gray-200 text-gray-500"
                    }`}
                  >
                    {PROTOCOL_LABELS[protocol] || protocol}
                  </button>
                ))}
              </div>
            ))}
          </div>
          <div className="flex gap-2 mt-3">
            <button type="button" className="btn-primary" disabled={busy || !chosen.length} onClick={create}>
              {busy ? t("settings.saving") : t("packages.trialCreate")}
            </button>
            <button type="button" className="btn-secondary" disabled={busy} onClick={() => setPicking(false)}>
              {t("common.cancel")}
            </button>
          </div>
        </div>
      )}

      {error && <div className="text-xs text-red-600 bg-red-50 dark:bg-red-500/10 dark:text-red-400 rounded-lg px-3 py-2 mt-3">{error}</div>}
    </div>
  );
}
