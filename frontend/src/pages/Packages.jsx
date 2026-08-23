import React, { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, Power, Package as PackageIcon, Server, Paperclip, Download, Check, X, Tag } from "lucide-react";
import Layout from "../components/Layout.jsx";
import MoneyInput from "../components/MoneyInput.jsx";
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
  setMyPackagePrice,
} from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";
import { useAuth } from "../context/AuthContext.jsx";

const emptyForm = {
  name: "",
  quota_gb: 20,
  duration_days: 30,
  price: 0,
  cooperation_price: "",
  description: "",
  enabled: true,
  bot_enabled: true,
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

const PROTOCOL_LABELS = { wireguard: "WireGuard", openvpn: "OpenVPN", l2tp: "L2TP", ikev2: "IKEv2", sstp: "SSTP", xray: "V2Ray/Xray" };

// Which protocols a server can actually carry. Same split the bot already
// applies (telegram_bot/keyboards.py's protocols_kb) - a MikroTik cannot
// serve Xray and an Xray node serves nothing else, so offering the full
// list here only ever produced a package whose service fails at
// provisioning time, long after anyone would connect the two.
const MIKROTIK_PROTOCOLS = ["wireguard", "openvpn", "l2tp", "ikev2", "sstp"];
const XRAY_PROTOCOLS = ["xray"];
const protocolsForType = (type) => (type === "xray" ? XRAY_PROTOCOLS : MIKROTIK_PROTOCOLS);

function formatToman(n) {
  return new Intl.NumberFormat("fa-IR").format(n || 0);
}

export default function Packages() {
  const { t } = useLanguage();
  const { role, wallet } = useAuth();
  const isSeller = role === "seller";
  const [items, setItems] = useState([]);
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

  const load = () => fetchPackages().then((res) => setItems(res.data));
  useEffect(() => {
    load();
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
      setError(err?.response?.data?.detail || t("packages.uploadError"));
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
      setError(err?.response?.data?.detail || t("packages.saveError"));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (id) => {
    if (!confirm(t("packages.deleteConfirm"))) return;
    await deletePackage(id);
    load();
  };

  const onToggle = async (pkg) => {
    await updatePackage(pkg.id, { enabled: !pkg.enabled });
    load();
  };

  const startEditPrice = (pkg) => {
    setEditingPriceId(pkg.id);
    setPriceDraft(pkg.my_price != null ? String(pkg.my_price) : String(pkg.price));
  };
  const cancelEditPrice = () => {
    setEditingPriceId(null);
    setPriceDraft("");
  };
  const saveMyPrice = async (pkgId) => {
    setPriceSaving(true);
    try {
      await setMyPackagePrice(pkgId, priceDraft === "" ? null : Number(priceDraft));
      await load();
      cancelEditPrice();
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
        <div className="flex justify-end mb-4">
          <button className="btn-primary" onClick={openCreate}>
            <Plus size={16} /> {t("packages.newPackage")}
          </button>
        </div>
      )}
      {isSeller && (
        <div className="text-xs text-gray-400 mb-4">{t("packages.sellerPriceHint")}</div>
      )}

      <div className="card !p-0 overflow-x-auto">
        <table className="w-full text-sm min-w-[48rem]">
          <thead className="bg-gray-50 text-gray-500 text-xs">
            <tr>
              <th className="text-right font-medium px-4 py-3">{t("packages.colName")}</th>
              <th className="text-right font-medium px-4 py-3">{t("packages.colQuota")}</th>
              <th className="text-right font-medium px-4 py-3">{t("packages.colDuration")}</th>
              <th className="text-right font-medium px-4 py-3">{t("packages.colPrice")}</th>
              <th className="text-right font-medium px-4 py-3">{t("packages.colStatus")}</th>
              <th className="text-right font-medium px-4 py-3">{t("packages.colActions")}</th>
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
                      <Server size={12} /> {t("packages.bundledServices", { count: p.connections.length })}
                      {p.max_concurrent_sessions ? t("packages.maxConcurrent", { count: p.max_concurrent_sessions }) : ""}
                    </div>
                  )}
                  {p.speed_limit_mbps ? (
                    <div className="text-xs text-amber-600 mt-1">{t("packages.speedLimitBadge", { mbps: p.speed_limit_mbps })}</div>
                  ) : null}
                </td>
                <td className="px-4 py-3 text-gray-600">{p.quota_gb ? `${p.quota_gb} GB` : t("packages.unlimited")}</td>
                <td className="px-4 py-3 text-gray-600">{p.duration_days ? t("packages.days", { count: p.duration_days }) : t("packages.noExpiry")}</td>
                <td className="px-4 py-3 text-gray-600" dir="ltr">
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
                    <div className="flex items-center gap-1" dir="ltr">
                      <MoneyInput
                        autoFocus
                        small
                        className="w-28"
                        value={priceDraft}
                        onChange={setPriceDraft}
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
                  )}
                </td>
                <td className="px-4 py-3">
                  <div className="flex flex-col gap-1 items-start">
                    <span className={`badge ${p.enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                      {t("packages.webPanel")}: {p.enabled ? t("status.active") : t("status.disabled")}
                    </span>
                    <span className={`badge ${p.bot_enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                      {t("packages.bot")}: {p.bot_enabled ? t("status.active") : t("status.disabled")}
                    </span>
                  </div>
                </td>
                <td className="px-4 py-3">
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
            {items.length === 0 && (
              <tr>
                <td colSpan={6} className="text-center text-gray-400 py-10">
                  <PackageIcon size={28} className="mx-auto mb-2 text-gray-300" />
                  {t("packages.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
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
              <div className="text-xs text-gray-400 mt-1">{t("packages.quotaHint")}</div>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldDuration")}</label>
              <input type="number" min="0" className="input" value={form.duration_days ?? ""} onChange={(e) => set("duration_days", e.target.value ? Number(e.target.value) : null)} />
              <div className="text-xs text-gray-400 mt-1">{t("packages.durationHint")}</div>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldPrice")}</label>
              <MoneyInput value={form.price} onChange={(v) => set("price", v === "" ? 0 : Number(v))} />
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("packages.fieldCooperationPrice")}</label>
            <MoneyInput
              placeholder={t("packages.cooperationPricePlaceholder")}
              value={form.cooperation_price}
              onChange={(v) => set("cooperation_price", v)}
            />
            <div className="text-xs text-gray-400 mt-1">
              {t("packages.cooperationHint")}
            </div>
            {costFloor !== null && (
              <div className={`text-xs mt-1 ${belowFloor ? "text-red-600 font-medium" : "text-gray-400"}`}>
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
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
              {t("packages.showInPanel")}
            </label>
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={form.bot_enabled} onChange={(e) => set("bot_enabled", e.target.checked)} />
              {t("packages.showInBot")}
            </label>
          </div>

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
            <div className="text-xs text-gray-400 mt-1">
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
            <div className="text-xs text-gray-400 mt-1">{t("packages.speedLimitHint")}</div>
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

          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
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
    </Layout>
  );
}
