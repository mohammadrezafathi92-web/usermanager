import React, { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, Power, GraduationCap, ImagePlus, Video, Download } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import {
  fetchTutorials,
  createTutorial,
  updateTutorial,
  deleteTutorial,
  uploadTutorialMedia,
  deleteTutorialMedia,
} from "../api/client.js";
import { useLanguage } from "../context/LanguageContext.jsx";

const emptyForm = { title: "", text: "", enabled: true, sort_order: 0 };

function formatFileSize(bytes) {
  if (!bytes) return "0 KB";
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(0)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

export default function Tutorials() {
  const { t } = useLanguage();
  const [items, setItems] = useState([]);
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(emptyForm);
  const [editingMedia, setEditingMedia] = useState([]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);

  const load = () => fetchTutorials().then((res) => setItems(res.data));
  useEffect(() => {
    load();
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const openCreate = () => {
    setEditingId(null);
    setForm(emptyForm);
    setEditingMedia([]);
    setError("");
    setOpen(true);
  };

  const openEdit = (tut) => {
    setEditingId(tut.id);
    setForm({ title: tut.title, text: tut.text || "", enabled: tut.enabled, sort_order: tut.sort_order });
    setEditingMedia(tut.media || []);
    setError("");
    setOpen(true);
  };

  const onUploadMedia = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !editingId) return;
    setUploading(true);
    setError("");
    try {
      const res = await uploadTutorialMedia(editingId, file);
      setEditingMedia((media) => [...media, res.data]);
    } catch (err) {
      setError(err?.response?.data?.detail || t("tutorials.uploadError"));
    } finally {
      setUploading(false);
    }
  };

  const onDeleteMedia = async (mediaId) => {
    if (!editingId) return;
    await deleteTutorialMedia(editingId, mediaId);
    setEditingMedia((media) => media.filter((m) => m.id !== mediaId));
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      if (editingId) {
        await updateTutorial(editingId, form);
      } else {
        await createTutorial(form);
      }
      setOpen(false);
      load();
    } catch (err) {
      setError(err?.response?.data?.detail || t("tutorials.saveError"));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (id) => {
    if (!confirm(t("tutorials.deleteConfirm"))) return;
    await deleteTutorial(id);
    load();
  };

  const onToggle = async (tut) => {
    await updateTutorial(tut.id, { enabled: !tut.enabled });
    load();
  };

  return (
    <Layout>
      <Topbar title={t("tutorials.title")} subtitle={t("tutorials.subtitle")} />

      <div className="flex justify-end mb-4">
        <button className="btn-primary" onClick={openCreate}>
          <Plus size={16} /> {t("tutorials.newTutorial")}
        </button>
      </div>

      <div className="card !p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 text-xs">
            <tr>
              <th className="text-right font-medium px-4 py-3">{t("tutorials.colTitle")}</th>
              <th className="text-right font-medium px-4 py-3">{t("tutorials.colMedia")}</th>
              <th className="text-right font-medium px-4 py-3">{t("tutorials.colOrder")}</th>
              <th className="text-right font-medium px-4 py-3">{t("tutorials.colStatus")}</th>
              <th className="text-right font-medium px-4 py-3">{t("tutorials.colActions")}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id} className="border-t border-gray-50 hover:bg-gray-50/60">
                <td className="px-4 py-3">
                  <div className="font-medium text-gray-800">{item.title}</div>
                  {item.text && <div className="text-xs text-gray-400 truncate max-w-md">{item.text}</div>}
                </td>
                <td className="px-4 py-3 text-gray-500">{t("tutorials.fileCount", { count: item.media?.length || 0 })}</td>
                <td className="px-4 py-3 text-gray-500">{item.sort_order}</td>
                <td className="px-4 py-3">
                  <span className={`badge ${item.enabled ? "bg-emerald-50 text-emerald-600" : "bg-gray-100 text-gray-500"}`}>
                    {item.enabled ? t("status.active") : t("status.disabled")}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <button title={item.enabled ? t("tutorials.disable") : t("tutorials.enable")} onClick={() => onToggle(item)} className="text-gray-400 hover:text-brand-600">
                      <Power size={16} />
                    </button>
                    <button title={t("tutorials.editTitle")} onClick={() => openEdit(item)} className="text-gray-400 hover:text-brand-600">
                      <Pencil size={16} />
                    </button>
                    <button title={t("tutorials.deleteTitle")} onClick={() => onDelete(item.id)} className="text-gray-400 hover:text-red-600">
                      <Trash2 size={16} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-gray-400 py-10">
                  <GraduationCap size={28} className="mx-auto mb-2 text-gray-300" />
                  {t("tutorials.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <Modal open={open} onClose={() => setOpen(false)} title={editingId ? t("tutorials.editModal") : t("tutorials.newModal")} width="max-w-2xl">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("tutorials.fieldTitle")}</label>
            <input className="input" required placeholder={t("tutorials.fieldTitlePlaceholder")} value={form.title} onChange={(e) => set("title", e.target.value)} />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("tutorials.fieldText")}</label>
            <textarea className="input" rows={5} value={form.text} onChange={(e) => set("text", e.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("tutorials.fieldOrder")}</label>
              <input type="number" className="input" value={form.sort_order} onChange={(e) => set("sort_order", Number(e.target.value))} />
            </div>
            <label className="flex items-center gap-2 mt-6 text-sm text-gray-600">
              <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
              {t("tutorials.fieldEnabled")}
            </label>
          </div>

          <div className="border-t border-gray-100 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-medium text-gray-700">{t("tutorials.mediaHeading")}</div>
              {editingId ? (
                <label className="btn-secondary cursor-pointer">
                  <ImagePlus size={14} /> {uploading ? t("tutorials.uploading") : t("tutorials.addMedia")}
                  <input type="file" accept="image/*,video/*" className="hidden" onChange={onUploadMedia} disabled={uploading} />
                </label>
              ) : null}
            </div>
            {!editingId && (
              <div className="text-xs text-gray-400">{t("tutorials.saveFirst")}</div>
            )}
            {editingId && editingMedia.length === 0 && (
              <div className="text-xs text-gray-400">{t("tutorials.noMedia")}</div>
            )}
            {editingMedia.map((m) => (
              <div key={m.id} className="flex items-center justify-between gap-2 py-1.5 px-2 rounded-lg bg-gray-50 mb-1.5 text-sm">
                <div className="flex items-center gap-2 text-gray-700 truncate">
                  {m.kind === "video" ? (
                    <Video size={14} className="text-gray-400 shrink-0" />
                  ) : (
                    <Download size={14} className="text-gray-400 shrink-0" />
                  )}
                  <span className="truncate">{m.filename}</span>
                  <span className="text-xs text-gray-400 shrink-0">({formatFileSize(m.size_bytes)})</span>
                </div>
                <button type="button" className="text-gray-400 hover:text-red-600 shrink-0" onClick={() => onDeleteMedia(m.id)}>
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
              {saving ? t("common.saving") : t("tutorials.saveTutorial")}
            </button>
          </div>
        </form>
      </Modal>
    </Layout>
  );
}
