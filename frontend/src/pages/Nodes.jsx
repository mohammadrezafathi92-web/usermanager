import React, { useEffect, useState } from "react";
import { Plus, Trash2, Pencil, Wifi, Globe, PlugZap, CheckCircle2, XCircle, Power, X } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import { fetchNodes, createNode, updateNode, deleteNode, testNode, pushRadiusConfig, pushSstpConfig, pushL2tpConfig, pushIkev2Config, importPppUsers, importUserManagerUsers, import3xuiClients } from "../api/client.js";
import { formatDateTime } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

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
  mt_sstp_port: 443,
  mt_sstp_certificate: "",
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
  const { t, language } = useLanguage();
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
  const [sstpStatus, setSstpStatus] = useState("");
  const [l2tpStatus, setL2tpStatus] = useState("");
  const [ikev2Status, setIkev2Status] = useState("");
  const [importStatus, setImportStatus] = useState("");
  const [importResult, setImportResult] = useState(null);
  const [umImportStatus, setUmImportStatus] = useState("");
  const [umImportResult, setUmImportResult] = useState(null);
  const [xuiImportStatus, setXuiImportStatus] = useState("");
  const [xuiImportResult, setXuiImportResult] = useState(null);
  const [togglingId, setTogglingId] = useState(null);
  // Step-by-step wizard state (see stepsFor/stepIsValid below) - instead of
  // dumping every MikroTik/WireGuard/RADIUS/Xray field on screen at once,
  // the create/edit modal now walks through one logical group of settings
  // at a time and won't let you move past a step until that step's
  // required fields are filled in.
  const [step, setStep] = useState(0);
  // UI-only list backing the single mt_client_dns text column (still a
  // plain comma-separated string on the backend/model - WireGuard itself
  // accepts multiple comma-separated DNS servers natively, see
  // services/link_builder.py - this is purely a nicer editor for that same
  // field, no schema/API change needed). Kept separate from `form` so a
  // row can sit blank while being typed into without vanishing (see
  // dnsToPayload, which is what actually gets filtered/joined on submit).
  const [dnsRows, setDnsRows] = useState(["1.1.1.1"]);

  const load = () => fetchNodes().then((res) => setNodes(res.data));
  useEffect(() => {
    load();
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const parseDns = (raw) => {
    const rows = (raw || "").split(",").map((s) => s.trim()).filter(Boolean);
    return rows.length ? rows : [""];
  };
  const updateDnsRow = (i, val) => setDnsRows((rows) => rows.map((r, idx) => (idx === i ? val : r)));
  const addDnsRow = () => setDnsRows((rows) => [...rows, ""]);
  const removeDnsRow = (i) => setDnsRows((rows) => (rows.length > 1 ? rows.filter((_, idx) => idx !== i) : rows));
  const dnsToPayload = () => dnsRows.map((r) => r.trim()).filter(Boolean).join(", ");

  // Shared by openCreate/openEdit so a leftover import/RADIUS result from a
  // previous edit session can never leak into a later create/edit modal -
  // previously only openEdit reset these, so closing an edit right after an
  // import and then clicking "افزودن نود" showed the old node's stale
  // result until an import button was clicked again.
  const resetModalStatus = () => {
    setError("");
    setRadiusStatus("");
    setSstpStatus("");
    setL2tpStatus("");
    setIkev2Status("");
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
    setDnsRows(parseDns(emptyForm.mt_client_dns));
    resetModalStatus();
    setStep(0);
    setOpen(true);
  };

  const openEdit = (node) => {
    setEditingId(node.id);
    setForm({ ...emptyForm, ...node });
    setDnsRows(parseDns(node.mt_client_dns));
    resetModalStatus();
    setStep(0);
    setOpen(true);
  };

  // Which wizard steps exist for the currently-selected server type, and
  // what each one is called - the "basic" step (name + type picker) is
  // shared by both types and always comes first.
  const stepsFor = (type) =>
    type === "mikrotik"
      ? [
          { key: "basic", label: t("nodes.stepBasic") },
          { key: "connection", label: t("nodes.stepConnection") },
          { key: "wireguard", label: t("nodes.stepWireguard") },
          { key: "radius", label: t("nodes.stepRadius") },
        ]
      : [
          { key: "basic", label: t("nodes.stepBasic") },
          { key: "connection", label: t("nodes.stepConnection") },
          { key: "public", label: t("nodes.stepPublic") },
        ];
  const steps = stepsFor(form.type);

  // Only the fields that are already marked `required` on their <input>
  // below are actually gated - the wizard doesn't invent new requirements,
  // it just stops you from skipping past the ones that already existed.
  const stepIsValid = (idx) => {
    const key = steps[idx]?.key;
    if (key === "basic") return !!form.name.trim();
    if (form.type === "mikrotik") {
      if (key === "connection") {
        return !!form.mt_host.trim() && !!form.mt_username.trim() && !!form.mt_password.trim() && !!form.mt_endpoint_host.trim();
      }
    } else if (key === "connection") {
      return form.xr_panel_mode === "3xui" ? !!form.xr_panel_base_url.trim() : !!form.xr_ssh_host.trim();
    }
    return true; // wireguard/radius/public steps have no required fields
  };

  const goNext = () => {
    if (!stepIsValid(step)) return;
    setStep((s) => Math.min(s + 1, steps.length - 1));
  };
  const goBack = () => setStep((s) => Math.max(s - 1, 0));

  const onImportPpp = async () => {
    if (!editingId) return;
    if (!confirm(t("nodes.importPppConfirm"))) return;
    setImportStatus("loading");
    setImportResult(null);
    try {
      const res = await importPppUsers(editingId);
      setImportResult(res.data);
      setImportStatus("done");
    } catch (err) {
      setImportStatus(err?.response?.data?.detail || t("nodes.importError"));
    }
  };

  const onImportUm = async () => {
    if (!editingId) return;
    if (!confirm(t("nodes.importUmConfirm"))) return;
    setUmImportStatus("loading");
    setUmImportResult(null);
    try {
      const res = await importUserManagerUsers(editingId);
      setUmImportResult(res.data);
      setUmImportStatus("done");
    } catch (err) {
      setUmImportStatus(err?.response?.data?.detail || t("nodes.importError"));
    }
  };

  const onImportXui = async () => {
    if (!editingId) return;
    if (!confirm(t("nodes.importXuiConfirm"))) return;
    setXuiImportStatus("loading");
    setXuiImportResult(null);
    try {
      const res = await import3xuiClients(editingId);
      setXuiImportResult(res.data);
      setXuiImportStatus("done");
    } catch (err) {
      setXuiImportStatus(err?.response?.data?.detail || t("nodes.importError"));
    }
  };

  const onPushRadius = async () => {
    if (!editingId) return;
    setRadiusStatus("loading");
    try {
      const res = await pushRadiusConfig(editingId, radiusPanelHost, radiusInterimUpdate);
      setRadiusStatus(res?.data?.message || t("nodes.pushSuccess"));
    } catch (err) {
      setRadiusStatus(err?.response?.data?.detail || t("nodes.radiusError"));
    }
  };

  const onPushSstp = async () => {
    if (!editingId) return;
    setSstpStatus("loading");
    try {
      const res = await pushSstpConfig(editingId, radiusPanelHost);
      setSstpStatus(res?.data?.message || t("nodes.pushSuccess"));
      load();
    } catch (err) {
      setSstpStatus(err?.response?.data?.detail || t("nodes.sstpError"));
    }
  };

  const onPushL2tp = async () => {
    if (!editingId) return;
    setL2tpStatus("loading");
    try {
      const res = await pushL2tpConfig(editingId, radiusPanelHost);
      setL2tpStatus(res?.data?.message || t("nodes.pushSuccess"));
      load();
    } catch (err) {
      setL2tpStatus(err?.response?.data?.detail || t("nodes.l2tpError"));
    }
  };

  const onPushIkev2 = async () => {
    if (!editingId) return;
    setIkev2Status("loading");
    try {
      const res = await pushIkev2Config(editingId, radiusPanelHost);
      setIkev2Status(res?.data?.message || t("nodes.pushSuccess"));
      load();
    } catch (err) {
      setIkev2Status(err?.response?.data?.detail || t("nodes.ikev2Error"));
    }
  };

  const submit = async (e) => {
    e.preventDefault();
    if (step < steps.length - 1) {
      // Enter/click on the primary button before the last step just
      // advances the wizard instead of saving - mirrors clicking "بعدی".
      goNext();
      return;
    }
    if (!stepIsValid(step)) return;
    setSaving(true);
    setError("");
    const payload = { ...form, mt_client_dns: dnsToPayload() };
    try {
      if (editingId) {
        await updateNode(editingId, payload);
      } else {
        await createNode(payload);
      }
      setOpen(false);
      setForm(emptyForm);
      setEditingId(null);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || t("nodes.saveError"));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (id) => {
    if (!confirm(t("nodes.deleteConfirm"))) return;
    try {
      await deleteNode(id);
      load();
    } catch (err) {
      alert(err?.response?.data?.detail || t("nodes.deleteError"));
    }
  };

  const onToggleEnabled = async (node) => {
    const next = !node.enabled;
    if (!next && !confirm(t("nodes.disableConfirm", { name: node.name }))) {
      return;
    }
    setTogglingId(node.id);
    // Optimistic update so the toggle feels instant; reconciled by load() below.
    setNodes((ns) => ns.map((n) => (n.id === node.id ? { ...n, enabled: next } : n)));
    try {
      await updateNode(node.id, { enabled: next });
    } catch (err) {
      alert(err?.response?.data?.detail || t("nodes.toggleError"));
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
      setTestResult((r) => ({ ...r, [id]: err?.response?.data?.detail || t("nodes.testError") }));
    }
  };

  return (
    <Layout>
      <Topbar title={t("nodes.title")} subtitle={t("nodes.subtitle")} />

      <div className="flex justify-end mb-4">
        <button className="btn-primary" onClick={openCreate}>
          <Plus size={16} /> {t("nodes.addServer")}
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
                      {n.enabled ? t("status.active") : t("status.disabled")}
                    </span>
                  </div>
                  <div className="text-xs text-gray-400">{n.type === "mikrotik" ? t("nodes.mikrotikType") : t("nodes.xrayType")}</div>
                </div>
              </div>
              <button
                type="button"
                title={n.enabled ? t("nodes.disableServer") : t("nodes.enableServer")}
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
              <div>{t("nodes.address", { value: n.type === "mikrotik" ? `${n.mt_host}:${n.mt_use_ssl ? n.mt_api_ssl_port : n.mt_port}${n.mt_use_ssl ? " (SSL)" : ""}` : (n.xr_panel_mode === "3xui" ? `${n.xr_panel_base_url} (${t("nodes.threexuiPanel")})` : n.xr_ssh_host) })}</div>
              <div>{t("nodes.lastSeen", { value: formatDateTime(n.last_seen, language) })}</div>
              {n.last_error && <div className="text-red-500">{t("nodes.error", { value: n.last_error })}</div>}
              {!n.enabled && <div className="text-amber-600">{t("nodes.disabledNote")}</div>}
            </div>

            <div className="flex items-center gap-2">
              <button className="btn-secondary flex-1" onClick={() => onTest(n.id)}>
                <PlugZap size={14} /> {t("nodes.testConnection")}
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
                {testResult[n.id] === "ok" ? t("nodes.testOk") : testResult[n.id]}
              </div>
            )}
          </div>
        ))}
        {nodes.length === 0 && <div className="card text-center text-gray-400 col-span-2 py-10">{t("nodes.empty")}</div>}
      </div>

      <Modal open={open} onClose={() => setOpen(false)} title={editingId ? t("nodes.editModal") : t("nodes.newModal")} width="max-w-2xl">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <div className="flex items-center gap-1.5 mb-2">
              {steps.map((s, i) => (
                <div key={s.key} className={`flex-1 h-1.5 rounded-full ${i <= step ? "bg-brand-500" : "bg-gray-200"}`} />
              ))}
            </div>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <span className="font-medium text-gray-700">{steps[step]?.label}</span>
              <span>{t("nodes.stepOf", { current: step + 1, total: steps.length })}</span>
            </div>
          </div>

          {step === 0 && (
            <>
              <div>
                <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldName")}</label>
                <input className="input" required value={form.name} onChange={(e) => set("name", e.target.value)} />
              </div>

              <div className="flex gap-2">
                <button type="button" disabled={!!editingId} onClick={() => set("type", "mikrotik")} className={`flex-1 rounded-xl border py-2 text-sm font-medium disabled:opacity-60 ${form.type === "mikrotik" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-gray-200 text-gray-500"}`}>
                  {t("nodes.mikrotikType")}
                </button>
                <button type="button" disabled={!!editingId} onClick={() => set("type", "xray")} className={`flex-1 rounded-xl border py-2 text-sm font-medium disabled:opacity-60 ${form.type === "xray" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-gray-200 text-gray-500"}`}>
                  {t("nodes.xrayType")}
                </button>
              </div>
            </>
          )}

          {form.type === "mikrotik" ? (
            <div className="space-y-4">
              {step === 1 && (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldRouterIp")}</label>
                  <input className="input" required value={form.mt_host} onChange={(e) => set("mt_host", e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldApiPort")}</label>
                  <input type="number" className="input" value={form.mt_port} onChange={(e) => set("mt_port", Number(e.target.value))} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldUsername")}</label>
                  <input className="input" required value={form.mt_username} onChange={(e) => set("mt_username", e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldPassword")}</label>
                  <input type="password" className="input" required value={form.mt_password} onChange={(e) => set("mt_password", e.target.value)} />
                </div>
                <div className="col-span-2">
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldEndpointHost")}</label>
                  <input className="input" required placeholder={t("nodes.endpointPlaceholder")} value={form.mt_endpoint_host} onChange={(e) => set("mt_endpoint_host", e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldApiSslPort")}</label>
                  <input type="number" className="input" value={form.mt_api_ssl_port} onChange={(e) => set("mt_api_ssl_port", Number(e.target.value))} />
                </div>
                <div className="col-span-2 flex items-center gap-2">
                  <input type="checkbox" id="mt_ssl" checked={form.mt_use_ssl} onChange={(e) => set("mt_use_ssl", e.target.checked)} />
                  <label htmlFor="mt_ssl" className="text-sm text-gray-600">{t("nodes.useApiSsl")}</label>
                </div>
              </div>
              )}

              {step === 2 && (
              <div className="border-t-0 pt-0">
                <div className="text-sm font-medium text-gray-700 mb-2">{t("nodes.wireguardSettings")}</div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldInterfaceName")}</label>
                    <input className="input" value={form.mt_wireguard_interface} onChange={(e) => set("mt_wireguard_interface", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldWgPort")}</label>
                    <input type="number" className="input" value={form.mt_endpoint_port} onChange={(e) => set("mt_endpoint_port", Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldClientSubnet")}</label>
                    <input className="input" value={form.mt_client_subnet} onChange={(e) => set("mt_client_subnet", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldClientDns")}</label>
                    <div className="space-y-1.5">
                      {dnsRows.map((row, i) => (
                        <div key={i} className="flex gap-1.5">
                          <input
                            className="input flex-1"
                            dir="ltr"
                            placeholder={t("nodes.dnsPlaceholder")}
                            value={row}
                            onChange={(e) => updateDnsRow(i, e.target.value)}
                          />
                          <button
                            type="button"
                            className="btn-secondary !px-2.5 shrink-0"
                            title={t("nodes.removeDns")}
                            onClick={() => removeDnsRow(i)}
                            disabled={dnsRows.length <= 1}
                          >
                            <X size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                    <button type="button" className="btn-secondary mt-1.5 text-xs" onClick={addDnsRow}>
                      <Plus size={13} /> {t("nodes.addDns")}
                    </button>
                  </div>
                </div>
              </div>
              )}

              {step === 3 && (
              <div className="border-t-0 pt-0">
                <div className="text-sm font-medium text-gray-700 mb-2">{t("nodes.radiusSectionTitle")}</div>
                <p className="text-xs text-gray-400 mb-3">
                  {t("nodes.radiusSectionNote")}
                </p>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldRadiusSecret")}</label>
                    <input className="input" placeholder={t("nodes.radiusSecretPlaceholder")} value={form.mt_radius_secret} onChange={(e) => set("mt_radius_secret", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldOvpnPort")}</label>
                    <input type="number" className="input" value={form.mt_ovpn_port} onChange={(e) => set("mt_ovpn_port", Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldOvpnCert")}</label>
                    <input className="input" placeholder={t("nodes.certPlaceholder")} value={form.mt_ovpn_certificate} onChange={(e) => set("mt_ovpn_certificate", e.target.value)} />
                  </div>
                  <div className="col-span-2 flex items-center gap-2">
                    <input type="checkbox" id="l2tp_ipsec" checked={form.mt_l2tp_use_ipsec} onChange={(e) => set("mt_l2tp_use_ipsec", e.target.checked)} />
                    <label htmlFor="l2tp_ipsec" className="text-sm text-gray-600">{t("nodes.l2tpIpsecLabel")}</label>
                  </div>
                  {form.mt_l2tp_use_ipsec && (
                    <div className="col-span-2">
                      <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldIpsecSecret")}</label>
                      <input className="input" value={form.mt_l2tp_ipsec_secret} onChange={(e) => set("mt_l2tp_ipsec_secret", e.target.value)} />
                    </div>
                  )}
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldIkev2Psk")}</label>
                    <input className="input" placeholder={t("nodes.ikev2PskPlaceholder")} value={form.mt_ikev2_psk} onChange={(e) => set("mt_ikev2_psk", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldSstpPort")}</label>
                    <input type="number" className="input" value={form.mt_sstp_port} onChange={(e) => set("mt_sstp_port", Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldSstpCert")}</label>
                    <input className="input" placeholder={t("nodes.certPlaceholder")} value={form.mt_sstp_certificate} onChange={(e) => set("mt_sstp_certificate", e.target.value)} />
                  </div>
                </div>

                {editingId && (
                  <div className="mt-3 bg-gray-50 rounded-lg p-3">
                    <div className="text-xs text-gray-500 mb-2">
                      {t("nodes.radiusPushNoteBefore")} <span dir="ltr">ppp aaa use-radius=yes</span> {t("nodes.radiusPushNoteAfter")}
                    </div>
                    <div className="mb-2">
                      <label className="block text-xs text-gray-500 mb-1">{t("nodes.fieldPanelHost")}</label>
                      <input
                        className="input w-full"
                        placeholder={t("nodes.panelHostPlaceholder")}
                        value={radiusPanelHost}
                        onChange={(e) => setRadiusPanelHost(e.target.value)}
                      />
                    </div>
                    <div className="mb-3">
                      <label className="block text-xs text-gray-500 mb-1">
                        {t("nodes.fieldInterimUpdate")}
                      </label>
                      <input
                        className="input w-full"
                        placeholder="00:01:00"
                        title={t("nodes.interimUpdateTitle")}
                        value={radiusInterimUpdate}
                        onChange={(e) => setRadiusInterimUpdate(e.target.value)}
                      />
                      <div className="text-xs text-gray-400 mt-1">
                        {t("nodes.interimUpdateHint")}
                      </div>
                    </div>
                    <button type="button" className="btn-secondary w-full" onClick={onPushRadius}>
                      {t("nodes.pushRadius")}
                    </button>
                    {radiusStatus && radiusStatus !== "loading" && (
                      <div className="text-xs mt-2 text-gray-600">{radiusStatus}</div>
                    )}

                    <div className="grid grid-cols-3 gap-2 mt-3 pt-3 border-t border-gray-200">
                      <div>
                        <button
                          type="button"
                          className="btn-secondary w-full !text-xs"
                          onClick={onPushSstp}
                          disabled={sstpStatus === "loading"}
                        >
                          {sstpStatus === "loading" ? "..." : t("nodes.pushSstp")}
                        </button>
                        {sstpStatus && sstpStatus !== "loading" && (
                          <div className="text-xs mt-1 text-gray-600 break-words">{sstpStatus}</div>
                        )}
                      </div>
                      <div>
                        <button
                          type="button"
                          className="btn-secondary w-full !text-xs"
                          onClick={onPushL2tp}
                          disabled={l2tpStatus === "loading"}
                        >
                          {l2tpStatus === "loading" ? "..." : t("nodes.pushL2tp")}
                        </button>
                        {l2tpStatus && l2tpStatus !== "loading" && (
                          <div className="text-xs mt-1 text-gray-600 break-words">{l2tpStatus}</div>
                        )}
                      </div>
                      <div>
                        <button
                          type="button"
                          className="btn-secondary w-full !text-xs"
                          onClick={onPushIkev2}
                          disabled={ikev2Status === "loading"}
                        >
                          {ikev2Status === "loading" ? "..." : t("nodes.pushIkev2")}
                        </button>
                        {ikev2Status && ikev2Status !== "loading" && (
                          <div className="text-xs mt-1 text-gray-600 break-words">{ikev2Status}</div>
                        )}
                      </div>
                    </div>
                    <div className="text-xs text-gray-400 mt-2">
                      {t("nodes.autoConfigNote")}
                    </div>
                  </div>
                )}

                {editingId && (
                  <div className="mt-3 bg-gray-50 rounded-lg p-3">
                    <div className="text-xs text-gray-500 mb-2">
                      {t("nodes.importPppNote")}
                    </div>
                    <button type="button" className="btn-secondary" onClick={onImportPpp} disabled={importStatus === "loading"}>
                      {importStatus === "loading" ? t("nodes.reading") : t("nodes.importPppButton")}
                    </button>
                    {importResult && (
                      <div className="text-xs mt-2 text-gray-600">
                        {t("nodes.importResult", { imported: importResult.imported_count, skipped: importResult.skipped_count })}
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
                      {t("nodes.importUmNote")}
                    </div>
                    <button type="button" className="btn-secondary" onClick={onImportUm} disabled={umImportStatus === "loading"}>
                      {umImportStatus === "loading" ? t("nodes.reading") : t("nodes.importUmButton")}
                    </button>
                    {umImportResult && (
                      <div className="text-xs mt-2 text-gray-600">
                        {t("nodes.importResult", { imported: umImportResult.imported_count, skipped: umImportResult.skipped_count })}
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
              )}
            </div>
          ) : (
            <div className="space-y-4">
              {step === 1 && (
              <>
              <div>
                <label className="block text-sm text-gray-600 mb-1">{t("nodes.connectionMethod")}</label>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => set("xr_panel_mode", "ssh")}
                    className={`flex-1 rounded-xl border py-2 text-sm font-medium ${form.xr_panel_mode !== "3xui" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-gray-200 text-gray-500"}`}
                  >
                    {t("nodes.sshMethod")}
                  </button>
                  <button
                    type="button"
                    onClick={() => set("xr_panel_mode", "3xui")}
                    className={`flex-1 rounded-xl border py-2 text-sm font-medium ${form.xr_panel_mode === "3xui" ? "border-brand-500 bg-brand-50 text-brand-700" : "border-gray-200 text-gray-500"}`}
                  >
                    {t("nodes.threexuiMethod")}
                  </button>
                </div>
                {form.xr_panel_mode === "3xui" && (
                  <p className="text-xs text-gray-400 mt-1">
                    {t("nodes.threexuiHint")}
                  </p>
                )}
              </div>

              {form.xr_panel_mode === "3xui" ? (
                <div className="grid grid-cols-2 gap-3">
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldPanelUrl")}</label>
                    <input
                      className="input"
                      placeholder="http://1.2.3.4:2053/xyzpanel"
                      required
                      value={form.xr_panel_base_url}
                      onChange={(e) => set("xr_panel_base_url", e.target.value)}
                    />
                  </div>
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldApiToken")}</label>
                    <input
                      className="input"
                      placeholder={t("nodes.apiTokenPlaceholder")}
                      value={form.xr_panel_api_token}
                      onChange={(e) => set("xr_panel_api_token", e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldPanelUsername")}</label>
                    <input className="input" value={form.xr_panel_username} onChange={(e) => set("xr_panel_username", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldPanelPassword")}</label>
                    <input type="password" className="input" value={form.xr_panel_password} onChange={(e) => set("xr_panel_password", e.target.value)} />
                  </div>
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldInboundId")}</label>
                    <input
                      type="number"
                      className="input"
                      placeholder={t("nodes.inboundIdPlaceholder")}
                      value={form.xr_panel_inbound_id ?? ""}
                      onChange={(e) => set("xr_panel_inbound_id", e.target.value ? Number(e.target.value) : null)}
                    />
                  </div>
                  {editingId && (
                    <div className="col-span-2 mt-1 bg-gray-50 rounded-lg p-3">
                      <div className="text-xs text-gray-500 mb-2">
                        {t("nodes.importXuiNote")}
                      </div>
                      <button type="button" className="btn-secondary" onClick={onImportXui} disabled={xuiImportStatus === "loading"}>
                        {xuiImportStatus === "loading" ? t("nodes.reading") : t("nodes.importXuiButton")}
                      </button>
                      {xuiImportResult && (
                        <div className="text-xs mt-2 text-gray-600">
                          {t("nodes.importResult", { imported: xuiImportResult.imported_count, skipped: xuiImportResult.skipped_count })}
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
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldSshHost")}</label>
                    <input className="input" required value={form.xr_ssh_host} onChange={(e) => set("xr_ssh_host", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldSshPort")}</label>
                    <input type="number" className="input" value={form.xr_ssh_port} onChange={(e) => set("xr_ssh_port", Number(e.target.value))} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldSshUsername")}</label>
                    <input className="input" value={form.xr_ssh_username} onChange={(e) => set("xr_ssh_username", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldSshPassword")}</label>
                    <input type="password" className="input" value={form.xr_ssh_password} onChange={(e) => set("xr_ssh_password", e.target.value)} />
                  </div>
                  <div className="col-span-2">
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldConfigPath")}</label>
                    <input className="input" value={form.xr_config_path} onChange={(e) => set("xr_config_path", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldServiceName")}</label>
                    <input className="input" value={form.xr_service_name} onChange={(e) => set("xr_service_name", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldApiAddress")}</label>
                    <input className="input" value={form.xr_api_address} onChange={(e) => set("xr_api_address", e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldInboundTag")}</label>
                    <input className="input" value={form.xr_inbound_tag} onChange={(e) => set("xr_inbound_tag", e.target.value)} />
                  </div>
                </div>
              )}
              </>
              )}

              {step === 2 && (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldPublicHost")}</label>
                  <input className="input" value={form.xr_public_host} onChange={(e) => set("xr_public_host", e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldPublicPort")}</label>
                  <input type="number" className="input" value={form.xr_public_port} onChange={(e) => set("xr_public_port", Number(e.target.value))} />
                </div>
                <div>
                  <label className="block text-sm text-gray-600 mb-1">{t("nodes.fieldSni")}</label>
                  <input className="input" value={form.xr_sni} onChange={(e) => set("xr_sni", e.target.value)} />
                </div>
              </div>
              )}
            </div>
          )}

          {error && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="flex justify-between gap-2 pt-2">
            <div>
              {step > 0 && (
                <button type="button" className="btn-secondary" onClick={goBack}>
                  {t("common.back")}
                </button>
              )}
            </div>
            <div className="flex gap-2">
              <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
                {t("common.cancel")}
              </button>
              <button type="submit" disabled={saving || !stepIsValid(step)} className="btn-primary">
                {step < steps.length - 1
                  ? t("common.next")
                  : saving
                  ? t("common.saving")
                  : t("nodes.saveServer")}
              </button>
            </div>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
