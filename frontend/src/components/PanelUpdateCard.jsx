import React, { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useAuth } from "../context/AuthContext.jsx";
import { checkPanelUpdate, applyPanelUpdate, fetchPanelUpdateStatus, resolvePanelUpdate } from "../api/client.js";

// The panel update follows the job's DURABLE state on the server (it
// survives the backend being replaced mid-update). A backend answering again
// means nothing by itself: success is shown only when the worker's own final
// state says completed for exactly the requested commit.
const FOLLOW = new Set(["queued", "starting", "running"]);
const POLL_MS = 4000;
const detail = (err) => err?.response?.data?.detail || "خطا";

export default function PanelUpdateCard({ t }) {
  const { build } = useAuth();
  const [info, setInfo] = useState(null);
  const [checking, setChecking] = useState(false);
  const [job, setJob] = useState(null);
  const [offline, setOffline] = useState(false);
  const [tick, setTick] = useState(0);              // every read, answered or not, schedules the next
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [ack, setAck] = useState(false);
  const [password, setPassword] = useState("");
  const busy = useRef(false);                       // one submit at a time (double click)
  const target = useRef(null);

  const read = useCallback(async () => {
    try {
      const res = await fetchPanelUpdateStatus();
      setOffline(false);
      setJob(res.data);
      return res.data;
    } catch (err) {
      // the backend restarting or the network gone: keep following
      if (!err?.response || err.response.status >= 500) setOffline(true);
      else setError(detail(err));
      return null;
    } finally {
      setTick((n) => n + 1);
    }
  }, []);

  useEffect(() => { read(); }, [read]);

  const following = !!job && FOLLOW.has(job.status);
  useEffect(() => {
    if (!following) return undefined;
    const timer = setTimeout(read, POLL_MS);
    return () => clearTimeout(timer);
  }, [following, tick, read]);

  const done = job?.status === "completed" && job.updated === true && !!job.commit && job.commit === job.target;
  useEffect(() => {
    if (!done || job.target !== target.current) return undefined;
    // only the run this page started reloads it (the new frontend)
    const timer = setTimeout(() => window.location.reload(), 3000);
    return () => clearTimeout(timer);
  }, [done, job]);

  const check = async () => {
    setChecking(true); setError("");
    try {
      const res = await checkPanelUpdate();
      setInfo(res.data);
    } catch (err) {
      setError(detail(err));
    } finally { setChecking(false); }
  };

  const submit = async () => {
    if (busy.current || !ack || !password || !info?.target) return;
    busy.current = true; setError("");
    try {
      const res = await applyPanelUpdate(info.target, password);
      target.current = info.target;
      setJob(res.data);
      setConfirming(false); setAck(false);
    } catch (err) {
      setError(detail(err));
      await read();                                   // a job may exist even though this answer was lost
    } finally {
      setPassword("");
      busy.current = false;
    }
  };

  const resolve = async () => {
    if (busy.current || !ack || !password || !job?.job_id) return;
    busy.current = true; setError("");
    try {
      const res = await resolvePanelUpdate(job.job_id, password);
      setJob(res.data); setAck(false);
    } catch (err) {
      setError(detail(err));
    } finally {
      setPassword("");
      busy.current = false;
    }
  };

  const blocked = following || job?.status === "unknown";
  const canStart = !!info?.target && !info.dirty && !info.up_to_date && !blocked;
  const confirmForm = (onSubmit, label, ackText) => (
    <div className="card-muted text-xs space-y-2 mb-3">
      <label className="flex items-start gap-2">
        <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <span>{ackText}</span>
      </label>
      <input type="password" className="input" autoComplete="current-password" value={password}
             aria-label={t("settings.updatePassword")} placeholder={t("settings.updatePassword")}
             onChange={(e) => setPassword(e.target.value)} />
      <div className="flex gap-2">
        <button type="button" className="btn-primary" disabled={!ack || !password} onClick={onSubmit}>{label}</button>
        <button type="button" className="btn-secondary" onClick={() => { setConfirming(false); setAck(false); setPassword(""); }}>
          {t("settings.updateCancel")}
        </button>
      </div>
    </div>
  );

  return (
    <div className="card mb-4">
      <div className="flex items-center gap-2 mb-3">
        <span className="w-7 h-7 rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-500/10 dark:text-brand-400 flex items-center justify-center shrink-0">
          <RefreshCw size={15} />
        </span>
        <h3 className="font-bold text-gray-700">{t("settings.updateTitle")}</h3>
      </div>
      <p className="text-xs text-gray-400 mb-4">{t("settings.updateHint")}</p>

      <div className="text-xs text-gray-500 mb-3" dir="ltr">
        {t("settings.updateCurrent")}: v{build?.version || "?"}{build?.commit ? ` · ${build.commit}` : ""}
      </div>

      {info?.dirty && (
        <div className="text-xs text-amber-700 bg-amber-50 dark:bg-amber-950 rounded-lg px-3 py-2 mb-3">
          {t("settings.updateDirty")}
        </div>
      )}
      {info && !info.dirty && (
        <div className={`text-sm mb-3 ${info.up_to_date ? "text-emerald-600" : "text-gray-700 dark:text-gray-200"}`}>
          {info.up_to_date ? t("settings.updateUpToDate") : t("settings.updateBehind", { count: info.behind })}
        </div>
      )}
      {info?.pending?.length > 0 && (
        <ul className="card-muted text-xs space-y-1 mb-3 max-h-40 overflow-y-auto" dir="ltr">
          {info.pending.map((line, i) => <li key={i} className="truncate">{line}</li>)}
        </ul>
      )}

      {following && (
        <div role="status" className="text-sm text-amber-600 mb-3">
          {job.status === "running"
            ? t("settings.updateRunningPhase", { phase: t(`settings.updatePhase.${job.phase}`) })
            : t(`settings.updateState.${job.status}`)}
          {job.overdue && <div className="text-xs">{t("settings.updateOverdue")}</div>}
          {offline && <div className="text-xs text-gray-500">{t("settings.updateReconnecting")}</div>}
        </div>
      )}
      {done && <div role="status" className="text-sm text-emerald-600 mb-3">{t("settings.updateDone", { commit: job.commit.slice(0, 12) })}</div>}
      {job?.status === "failed" && (
        <div role="alert" className="text-xs text-red-600 mb-3">
          {t("settings.updateFailed", { code: job.error_code, phase: t(`settings.updatePhase.${job.phase}`) })}
        </div>
      )}
      {job?.status === "abandoned" && <div className="text-xs text-amber-700 mb-3">{t("settings.updateAbandoned")}</div>}
      {job?.status === "unknown" && (
        <div role="alert" className="text-xs text-red-600 mb-3">
          {t("settings.updateUnknown", { code: job.error_code })}
        </div>
      )}
      {job?.resolvable && confirmForm(resolve, t("settings.updateResolve"), t("settings.updateResolveAck"))}
      {error && <div className="text-xs text-red-600 bg-red-50 dark:bg-red-950 rounded-lg px-3 py-2 mb-3 whitespace-pre-wrap" dir="ltr">{error}</div>}

      {confirming && canStart && (
        <>
          <div className="text-xs mb-2">{t("settings.updateTarget")}: <code dir="ltr">{info.target}</code></div>
          {confirmForm(submit, t("settings.updateStart"), t("settings.updateConfirmAck"))}
        </>
      )}

      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-secondary" disabled={checking || blocked} onClick={check}>
          {checking ? "..." : t("settings.updateCheck")}
        </button>
        <button type="button" className="btn-primary" disabled={!canStart || confirming}
                onClick={() => { setConfirming(true); setError(""); }}>
          {t("settings.updateApply")}
        </button>
      </div>
    </div>
  );
}
