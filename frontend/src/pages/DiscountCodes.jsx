import React, { useEffect, useState } from "react";
import { Plus, Trash2, Pencil, Ticket, Power } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import { fetchDiscountCodes, createDiscountCode, updateDiscountCode, deleteDiscountCode } from "../api/client.js";
import { formatDateTime } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

const EMPTY_FORM = { code: "", kind: "percent", value: 0, max_uses: "", enabled: true, expires_at: "", note: "" };

export default function DiscountCodes() {
  const { t, language } = useLanguage();
  const [codes, setCodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = () => {
    setLoading(true);
    fetchDiscountCodes()
      .then((res) => setCodes(res.data))
      .catch(() => setCodes([]))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setError("");
    setModalOpen(true);
  };

  const openEdit = (c) => {
    setEditing(c);
    setForm({
      code: c.code,
      kind: c.kind,
      value: c.value,
      max_uses: c.max_uses ?? "",
      enabled: c.enabled,
      expires_at: c.expires_at ? c.expires_at.slice(0, 16) : "",
      note: c.note || "",
    });
    setError("");
    setModalOpen(true);
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    const payload = {
      ...form,
      value: Number(form.value) || 0,
      max_uses: form.max_uses === "" ? null : Number(form.max_uses),
      expires_at: form.expires_at || null,
    };
    try {
      if (editing) {
        const { code, ...updatePayload } = payload; // code isn't editable after creation
        await updateDiscountCode(editing.id, updatePayload);
      } else {
        await createDiscountCode(payload);
      }
      setModalOpen(false);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || t("discountCodes.saveError"));
    } finally {
      setSaving(false);
    }
  };

  const toggleEnabled = async (c) => {
    await updateDiscountCode(c.id, { enabled: !c.enabled });
    load();
  };

  const remove = async (c) => {
    if (!window.confirm(t("discountCodes.confirmDelete", { code: c.code }))) return;
    await deleteDiscountCode(c.id);
    load();
  };

  return (
    <Layout>
      <Topbar title={t("discountCodes.title")} subtitle={t("discountCodes.subtitle")} />

      <div className="flex justify-end mb-4">
        <button className="btn-primary" onClick={openCreate}>
          <Plus size={16} /> {t("discountCodes.newCode")}
        </button>
      </div>

      <div className="card !p-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-400 border-b border-gray-50">
                <th className="text-right font-medium px-4 py-3">{t("discountCodes.colCode")}</th>
                <th className="text-right font-medium px-4 py-3">{t("discountCodes.colValue")}</th>
                <th className="text-right font-medium px-4 py-3">{t("discountCodes.colUsage")}</th>
                <th className="text-right font-medium px-4 py-3">{t("discountCodes.colExpires")}</th>
                <th className="text-right font-medium px-4 py-3">{t("discountCodes.colStatus")}</th>
                <th className="text-right font-medium px-4 py-3">{t("discountCodes.colNote")}</th>
                <th className="text-right font-medium px-4 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {codes.map((c) => (
                <tr key={c.id} className="border-t border-gray-50 hover:bg-gray-50/60">
                  <td className="px-4 py-3">
                    <span className="inline-flex items-center gap-1 font-mono font-medium text-gray-800">
                      <Ticket size={14} className="text-brand-500" /> {c.code}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-600">
                    {c.kind === "percent" ? `${c.value}%` : `${c.value.toLocaleString()} ${t("discountCodes.toman")}`}
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {c.used_count}
                    {c.max_uses ? ` / ${c.max_uses}` : ` / ${t("discountCodes.unlimited")}`}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{c.expires_at ? formatDateTime(c.expires_at, language) : t("discountCodes.never")}</td>
                  <td className="px-4 py-3">
                    <button onClick={() => toggleEnabled(c)} className={`badge ${c.enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                      <Power size={12} className="inline ml-1" />
                      {c.enabled ? t("discountCodes.enabled") : t("discountCodes.disabled")}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-gray-400 text-xs max-w-[12rem] truncate">{c.note || "-"}</td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-2">
                      <button onClick={() => openEdit(c)} className="text-gray-400 hover:text-brand-600">
                        <Pencil size={16} />
                      </button>
                      <button onClick={() => remove(c)} className="text-gray-400 hover:text-red-500">
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {!loading && codes.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-10 text-center text-gray-400">
                    {t("discountCodes.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <Modal open={modalOpen} onClose={() => setModalOpen(false)} title={editing ? t("discountCodes.editCode") : t("discountCodes.newCode")}>
        <form onSubmit={submit} className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">{t("discountCodes.colCode")}</label>
            <input
              className="input font-mono"
              dir="ltr"
              disabled={!!editing}
              placeholder="SUMMER20"
              value={form.code}
              onChange={(e) => setForm((f) => ({ ...f, code: e.target.value.toUpperCase() }))}
              required
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("discountCodes.kind")}</label>
            <select className="input" value={form.kind} onChange={(e) => setForm((f) => ({ ...f, kind: e.target.value }))}>
              <option value="percent">{t("discountCodes.kindPercent")}</option>
              <option value="fixed">{t("discountCodes.kindFixed")}</option>
            </select>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">
              {form.kind === "percent" ? t("discountCodes.valuePercent") : t("discountCodes.valueFixed")}
            </label>
            <input
              type="number" min="0" step={form.kind === "percent" ? "1" : "1000"} className="input" dir="ltr"
              value={form.value}
              onChange={(e) => setForm((f) => ({ ...f, value: e.target.value }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("discountCodes.maxUses")}</label>
            <input
              type="number" min="0" className="input" dir="ltr"
              placeholder={t("discountCodes.unlimited")}
              value={form.max_uses}
              onChange={(e) => setForm((f) => ({ ...f, max_uses: e.target.value }))}
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("discountCodes.expiresAt")}</label>
            <input
              type="datetime-local" className="input" dir="ltr"
              value={form.expires_at}
              onChange={(e) => setForm((f) => ({ ...f, expires_at: e.target.value }))}
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm text-gray-600 mb-1">{t("discountCodes.note")}</label>
            <input
              className="input"
              value={form.note}
              onChange={(e) => setForm((f) => ({ ...f, note: e.target.value }))}
            />
          </div>
          <div className="md:col-span-2 flex items-center gap-2">
            <input
              type="checkbox" id="dc-enabled" checked={form.enabled}
              onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
            />
            <label htmlFor="dc-enabled" className="text-sm text-gray-600">{t("discountCodes.enabled")}</label>
          </div>
          {error && <div className="md:col-span-2 text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{error}</div>}
          <div className="md:col-span-2">
            <button type="submit" disabled={saving} className="btn-primary">
              {saving ? t("settings.saving") : t("discountCodes.save")}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
