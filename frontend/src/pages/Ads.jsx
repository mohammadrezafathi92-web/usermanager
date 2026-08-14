import React, { useEffect, useState } from "react";
import { Megaphone, Plus, Pencil, Trash2, Send, Eye, Image as ImageIcon, X, Check, CalendarClock } from "lucide-react";
import Layout from "../components/Layout.jsx";
import Topbar from "../components/Topbar.jsx";
import Modal from "../components/Modal.jsx";
import {
  fetchAdChannel, updateAdChannel, fetchAdPlaceholders, fetchAdPosts, createAdPost,
  updateAdPost, deleteAdPost, previewAdPost, sendAdPostNow, uploadAdPostImage, deleteAdPostImage, fetchAdSchedule,
  fetchPackages, fetchDiscountCodes,
} from "../api/client.js";
import { formatDateTime } from "../utils.js";
import { useLanguage } from "../context/LanguageContext.jsx";

const EMPTY = { title: "", body: "", package_id: null, discount_code_id: null, button_text: "🛒 خرید و اطلاعات بیشتر", enabled: true };

export default function Ads() {
  const { t, language } = useLanguage();
  const [channel, setChannel] = useState(null);
  const [posts, setPosts] = useState([]);
  const [packages, setPackages] = useState([]);
  const [codes, setCodes] = useState([]);
  const [placeholders, setPlaceholders] = useState({});
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState("");
  const [schedule, setSchedule] = useState(null);

  // The schedule is derived from the channel settings AND the posts, so it
  // is refreshed alongside them rather than on its own timer - a stale
  // "next post at ..." is worse than none.
  const loadSchedule = () => fetchAdSchedule().then((r) => setSchedule(r.data)).catch(() => {});
  const loadPosts = () => fetchAdPosts().then((r) => setPosts(r.data)).then(loadSchedule);

  useEffect(() => {
    fetchAdChannel().then((r) => setChannel(r.data));
    fetchAdPlaceholders().then((r) => setPlaceholders(r.data));
    loadPosts();
    fetchPackages().then((r) => setPackages(r.data)).catch(() => {});
    fetchDiscountCodes().then((r) => setCodes(r.data)).catch(() => {});
  }, []);

  const saveChannel = async (patch) => {
    const next = { ...channel, ...patch };
    setChannel(next);                       // optimistic - a toggle that lags feels broken
    const res = await updateAdChannel(patch);
    setChannel(res.data);
    loadSchedule();
  };

  const openNew = () => { setEditingId(null); setForm(EMPTY); setOpen(true); };
  const openEdit = (p) => {
    setEditingId(p.id);
    setForm({ ...EMPTY, ...p });
    setOpen(true);
  };

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const payload = {
        title: form.title, body: form.body,
        package_id: form.package_id || null,
        discount_code_id: form.discount_code_id || null,
        button_text: form.button_text, enabled: form.enabled,
      };
      if (editingId) await updateAdPost(editingId, payload);
      else await createAdPost(payload);
      setOpen(false);
      loadPosts();
    } finally { setBusy(false); }
  };

  const onImage = async (postId, file) => {
    if (!file) return;
    await uploadAdPostImage(postId, file);
    loadPosts();
  };

  const showPreview = async (p) => {
    const res = await previewAdPost(p.id);
    setPreview({ ...res.data, title: p.title });
  };

  const sendNow = async (p) => {
    setBusy(true);
    try {
      await sendAdPostNow(p.id);
      setFlash(t("ads.sent"));
      setTimeout(() => setFlash(""), 3000);
      loadPosts();
      fetchAdChannel().then((r) => setChannel(r.data));
    } catch (err) {
      setFlash(err?.response?.data?.detail || "خطا");
      setTimeout(() => setFlash(""), 6000);
    } finally { setBusy(false); }
  };

  const remove = async (p) => {
    await deleteAdPost(p.id);
    loadPosts();
  };

  return (
    <Layout>
      <Topbar title={t("ads.title")} subtitle={t("ads.subtitle")} />

      {/* Channel setup first: an advert library is useless until the bot can
          actually reach a channel, so that decision comes before the posts. */}
      <div className="card mb-6">
        <div className="flex items-center gap-2 mb-3">
          <Megaphone size={18} className="text-brand-600" />
          <h3 className="font-bold text-gray-700">{t("ads.channelTitle")}</h3>
        </div>
        <p className="text-xs text-gray-400 mb-4">{t("ads.channelHint")}</p>

        {channel && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 items-start">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("ads.chatId")}</label>
              <input
                className="input" dir="ltr" placeholder="@my_vpn_channel"
                value={channel.chat_id || ""}
                onChange={(e) => setChannel({ ...channel, chat_id: e.target.value })}
                onBlur={(e) => saveChannel({ chat_id: e.target.value })}
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("ads.interval")}</label>
              <input
                type="number" min="1" max="336" className="input" dir="ltr"
                value={channel.interval_hours ?? 6}
                onChange={(e) => setChannel({ ...channel, interval_hours: Number(e.target.value) })}
                onBlur={(e) => saveChannel({ interval_hours: Number(e.target.value) })}
              />
            </div>

            <div className="sm:col-span-2">
              <label className="block text-sm text-gray-600 mb-1">{t("ads.activeHours")}</label>
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-400">{t("ads.from")}</span>
                <input
                  type="number" min="0" max="23" className="input-sm w-20" dir="ltr"
                  value={channel.active_from_hour ?? 9}
                  onChange={(e) => setChannel({ ...channel, active_from_hour: Number(e.target.value) })}
                  onBlur={(e) => saveChannel({ active_from_hour: Number(e.target.value) })}
                />
                <span className="text-xs text-gray-400">{t("ads.to")}</span>
                <input
                  type="number" min="0" max="23" className="input-sm w-20" dir="ltr"
                  value={channel.active_to_hour ?? 23}
                  onChange={(e) => setChannel({ ...channel, active_to_hour: Number(e.target.value) })}
                  onBlur={(e) => saveChannel({ active_to_hour: Number(e.target.value) })}
                />
              </div>
              <div className="hint">{t("ads.activeHoursHint")}</div>
            </div>

            <div className="sm:col-span-2 space-y-2 pt-1">
              <label className="flex items-center gap-2 text-sm text-gray-600">
                <input type="checkbox" checked={!!channel.enabled} onChange={(e) => saveChannel({ enabled: e.target.checked })} />
                {t("ads.enabled")}
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-600">
                <input type="checkbox" checked={!!channel.auto_send} onChange={(e) => saveChannel({ auto_send: e.target.checked })} />
                {t("ads.autoSend")}
              </label>
              <div className="hint">{t("ads.autoSendHint")}</div>
              <label className="flex items-center gap-2 text-sm text-gray-600">
                <input type="checkbox" checked={!!channel.delete_previous} onChange={(e) => saveChannel({ delete_previous: e.target.checked })} />
                {t("ads.deletePrevious")}
              </label>
            </div>

            <div className="sm:col-span-2 flex flex-wrap gap-4 text-xs text-gray-400 pt-2 border-t border-gray-100 dark:border-slate-800">
              <span>{t("ads.lastSent")}: {channel.last_sent_at ? formatDateTime(channel.last_sent_at, language) : t("ads.never")}</span>
              <span>{t("ads.sentCount")}: <span className="tnum">{channel.sent_count || 0}</span></span>
            </div>

            {/* The single most common failure is the bot not being a channel
                admin, which Telegram only reveals at send time. */}
            {channel.last_error && (
              <div className="sm:col-span-2 text-xs text-red-600 bg-red-50 dark:bg-red-950 rounded-lg px-3 py-2" dir="ltr">
                {channel.last_error}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Answers the two questions an admin actually has: how many adverts
          are in rotation, and which one goes out when. Produced by
          simulating the real rotation on the server, so it cannot drift
          from what the scheduler does. */}
      {schedule && (
        <div className="card mb-6">
          <div className="flex items-center gap-2 mb-1">
            <CalendarClock size={18} className="text-brand-600" />
            <h3 className="font-bold text-gray-700">{t("ads.schedule")}</h3>
          </div>
          <div className="text-xs text-gray-400 mb-3">
            {t("ads.scheduleSummary", {
              enabled: schedule.enabled_posts,
              total: schedule.total_posts,
              hours: schedule.interval_hours,
            })}
            {!schedule.in_window_now && <span className="text-amber-600"> · {t("ads.outsideWindow")}</span>}
          </div>
          {schedule.upcoming?.length ? (
            <ol className="space-y-1.5">
              {schedule.upcoming.map((s, i) => (
                <li key={i} className="flex items-center gap-3 text-sm">
                  <span className="w-6 h-6 rounded-lg bg-gray-100 dark:bg-slate-800 text-xs text-gray-500 flex items-center justify-center shrink-0 tnum">
                    {i + 1}
                  </span>
                  <span className="text-gray-500 tnum shrink-0" dir="ltr">{formatDateTime(s.at, language)}</span>
                  <span className="text-gray-800 dark:text-gray-100 truncate">{s.title}</span>
                </li>
              ))}
            </ol>
          ) : (
            <div className="empty-state">{t("ads.noSchedule")}</div>
          )}
        </div>
      )}

      <div className="flex items-center justify-between mb-2 gap-2">
        <div>
          <div className="section-title">{t("ads.postsTitle")}</div>
          <div className="hint">{t("ads.postsHint")}</div>
        </div>
        <button className="btn-primary shrink-0" onClick={openNew}>
          <Plus size={16} /> {t("ads.newPost")}
        </button>
      </div>

      {flash && <div className="mb-3 text-sm text-emerald-700 bg-emerald-50 dark:bg-emerald-950 rounded-lg px-3 py-2">{flash}</div>}

      {posts.length === 0 ? (
        <div className="card empty-state">{t("ads.none")}</div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 sm:gap-4">
          {posts.map((p) => (
            <div key={p.id} className={`card ${p.enabled ? "" : "opacity-60"}`}>
              <div className="flex items-start justify-between gap-2 mb-2">
                <div className="min-w-0">
                  <div className="font-medium text-gray-800 truncate">{p.title || `#${p.id}`}</div>
                  <div className="text-xs text-gray-400">
                    {p.last_sent_at ? formatDateTime(p.last_sent_at, language) : t("ads.never")}
                    {" · "}
                    <span className="tnum">{p.sent_count || 0}</span>
                  </div>
                </div>
                <label className="flex items-center gap-1 text-xs text-gray-500 shrink-0">
                  <input
                    type="checkbox" checked={!!p.enabled}
                    onChange={async (e) => { await updateAdPost(p.id, { enabled: e.target.checked }); loadPosts(); }}
                  />
                  {t("ads.enabled")}
                </label>
              </div>

              <div className="card-muted text-sm whitespace-pre-wrap break-words max-h-32 overflow-y-auto mb-3">
                {p.body || "—"}
              </div>

              <div className="flex flex-wrap gap-2">
                <button className="btn-secondary btn-sm" onClick={() => showPreview(p)}><Eye size={14} /> {t("ads.preview")}</button>
                <button className="btn-primary btn-sm" disabled={busy} onClick={() => sendNow(p)}><Send size={14} /> {t("ads.sendNow")}</button>
                <button className="btn-secondary btn-sm" onClick={() => openEdit(p)}><Pencil size={14} /></button>
                <label className="btn-secondary btn-sm cursor-pointer">
                  <ImageIcon size={14} />
                  <input type="file" accept="image/*" className="hidden" onChange={(e) => onImage(p.id, e.target.files?.[0])} />
                </label>
                {p.image_name && (
                  <button className="btn-ghost btn-sm text-gray-400" onClick={async () => { await deleteAdPostImage(p.id); loadPosts(); }}>
                    <X size={14} /> {p.image_name}
                  </button>
                )}
                <button className="btn-danger btn-sm" onClick={() => remove(p)}><Trash2 size={14} /></button>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal open={open} onClose={() => setOpen(false)} title={editingId ? t("ads.editPost") : t("ads.newPost")} width="max-w-2xl">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("ads.postTitle")}</label>
            <input className="input" value={form.title || ""} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("ads.body")}</label>
            <textarea className="input-area" rows={7} required value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} />
            <div className="hint">
              {t("ads.placeholders")}:{" "}
              {Object.entries(placeholders).map(([k, label]) => (
                <button
                  key={k} type="button"
                  className="inline-block mx-0.5 px-1.5 rounded bg-gray-100 dark:bg-slate-800 hover:bg-brand-50 dark:hover:bg-brand-500/10"
                  title={label}
                  onClick={() => setForm((f) => ({ ...f, body: `${f.body}${k}` }))}
                >
                  <span dir="ltr">{k}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("ads.package")}</label>
              <select className="input" value={form.package_id || ""} onChange={(e) => setForm({ ...form, package_id: e.target.value ? Number(e.target.value) : null })}>
                <option value="">{t("ads.noPackage")}</option>
                {packages.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">{t("ads.discountCode")}</label>
              <select className="input" value={form.discount_code_id || ""} onChange={(e) => setForm({ ...form, discount_code_id: e.target.value ? Number(e.target.value) : null })}>
                <option value="">{t("ads.noCode")}</option>
                {codes.map((c) => <option key={c.id} value={c.id}>{c.code}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">{t("ads.buttonText")}</label>
            <input className="input" value={form.button_text || ""} onChange={(e) => setForm({ ...form, button_text: e.target.value })} />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>{t("common.cancel")}</button>
            <button type="submit" className="btn-primary" disabled={busy}><Check size={16} /> {t("common.save")}</button>
          </div>
        </form>
      </Modal>

      <Modal open={!!preview} onClose={() => setPreview(null)} title={t("ads.preview")}>
        <div className="card-muted whitespace-pre-wrap break-words text-sm">{preview?.text}</div>
        {preview?.has_image && <div className="hint mt-2"><ImageIcon size={12} className="inline" /> {t("ads.image")}</div>}
      </Modal>
    </Layout>
  );
}
