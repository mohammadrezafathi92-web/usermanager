import { translate } from "./i18n/translations.js";

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "-";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const idx = Math.min(i, units.length - 1);
  const value = bytes / Math.pow(1024, idx);
  return `${value.toFixed(value >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`;
}

export function formatBitrate(bytesPerSec) {
  // Dashboard's "میانگین سرعت مصرف" used to run this exact value through
  // formatBytes instead (e.g. "540 KB/s") - technically an accurate BYTE
  // count, but network speed is conventionally reported in BITS/sec
  // (Mbps) - what an ISP plan, a router's port speed, or a speedtest.net
  // result all use. Showing bytes made every real-world comparison look
  // off by a factor of 8 on top of a completely different unit, which is
  // exactly why this looked "wrong" next to reality even though the raw
  // number itself was correct.
  if (bytesPerSec === null || bytesPerSec === undefined) return "-";
  const bits = bytesPerSec * 8;
  if (bits <= 0) return "0 bps";
  const units = ["bps", "Kbps", "Mbps", "Gbps"];
  // Network speeds use decimal (SI, 1000-based) scaling - 1 Mbps really is
  // 1,000,000 bit/s, not 1,048,576 - unlike formatBytes' 1024-based scaling
  // for storage sizes. Using 1024 here would make the shown Mbps number
  // not match what a speed-test tool reports for the same real throughput.
  const i = Math.floor(Math.log(bits) / Math.log(1000));
  const idx = Math.min(Math.max(i, 0), units.length - 1);
  const value = bits / Math.pow(1000, idx);
  return `${value.toFixed(value >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`;
}

export function formatUptime(seconds) {
  if (!seconds || seconds < 0) return "-";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

export function gbToBytes(gb) {
  return Math.round(Number(gb) * 1024 * 1024 * 1024);
}

export function bytesToGb(bytes) {
  if (!bytes) return 0;
  return +(bytes / (1024 * 1024 * 1024)).toFixed(2);
}

// STATUS_LABELS used to be a plain Persian-only lookup object, which meant
// the status badge shown across Users/UserDetail always rendered in Persian
// even in English mode. It's now a function keyed off the same status.*
// translation keys already used elsewhere, so it follows the active language.
export function statusLabel(status, lang = "fa") {
  return translate(lang, `status.${status}`);
}

export const STATUS_STYLES = {
  active: "bg-emerald-50 text-emerald-600",
  disabled: "bg-gray-100 text-gray-500",
  quota_exceeded: "bg-amber-50 text-amber-600",
  expired: "bg-red-50 text-red-600",
};

// formatDate/formatDateTime used to hardcode the "fa-IR" locale (Jalali
// calendar + Persian digits) regardless of the active language, so every
// date on the page stayed Persian even in English mode. They now accept the
// active language and switch locale/fallback text accordingly.
export function formatDate(value, lang = "fa") {
  if (!value) return translate(lang, "userDetail.noExpiry");
  const d = new Date(value);
  return lang === "en"
    ? d.toLocaleDateString("en-US", { year: "numeric", month: "2-digit", day: "2-digit" })
    : d.toLocaleDateString("fa-IR", { year: "numeric", month: "2-digit", day: "2-digit" });
}

export function formatDateTime(value, lang = "fa") {
  if (!value) return "-";
  const d = new Date(value);
  return lang === "en" ? d.toLocaleString("en-US") : d.toLocaleString("fa-IR");
}

// navigator.clipboard.writeText only works in a "secure context" (https or
// localhost). Since this panel is often accessed over plain http://IP, we
// fall back to the older execCommand("copy") trick so the copy buttons
// actually work on http deployments too.
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      // fall through to the legacy fallback below
    }
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch (e) {
    return false;
  }
}

export function downloadTextFile(filename, content) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function downloadBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ---------- Jalali (شمسی) date conversion ----------
// The accounting section's date-range filters let the admin TYPE a Jalali
// date (e.g. 1405/05/18) which the API - which speaks ISO/Gregorian - needs
// converted, and shows stored ISO dates back in Jalali. Standard well-known
// jalaali algorithm, no external dependency.
const _div = (a, b) => ~~(a / b);

export function gregorianToJalali(gy, gm, gd) {
  const g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
  let jy = gy <= 1600 ? 0 : 979;
  gy -= gy <= 1600 ? 621 : 1600;
  const gy2 = gm > 2 ? gy + 1 : gy;
  let days = 365 * gy + _div(gy2 + 3, 4) - _div(gy2 + 99, 100) + _div(gy2 + 399, 400) - 80 + gd + g_d_m[gm - 1];
  jy += 33 * _div(days, 12053);
  days %= 12053;
  jy += 4 * _div(days, 1461);
  days %= 1461;
  if (days > 365) {
    jy += _div(days - 1, 365);
    days = (days - 1) % 365;
  }
  const jm = days < 186 ? 1 + _div(days, 31) : 7 + _div(days - 186, 30);
  const jd = 1 + (days < 186 ? days % 31 : (days - 186) % 30);
  return [jy, jm, jd];
}

export function jalaliToGregorian(jy, jm, jd) {
  let gy = jy <= 979 ? 621 : 1600;
  jy -= jy <= 979 ? 0 : 979;
  let days = 365 * jy + _div(jy, 33) * 8 + _div((jy % 33) + 3, 4) + 78 + jd + (jm < 7 ? (jm - 1) * 31 : (jm - 7) * 30 + 186);
  gy += 400 * _div(days, 146097);
  days %= 146097;
  if (days > 36524) {
    gy += 100 * _div(--days, 36524);
    days %= 36524;
    if (days >= 365) days++;
  }
  gy += 4 * _div(days, 1461);
  days %= 1461;
  if (days > 365) {
    gy += _div(days - 1, 365);
    days = (days - 1) % 365;
  }
  let gd = days + 1;
  const leap = (gy % 4 === 0 && gy % 100 !== 0) || gy % 400 === 0;
  const sal_a = [0, 31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  let gm;
  for (gm = 1; gm <= 12 && gd > sal_a[gm]; gm++) gd -= sal_a[gm];
  return [gy, gm, gd];
}

// "2026-08-09" (or full ISO datetime) -> "1405/05/18"
export function isoToJalali(iso) {
  if (!iso) return "";
  const [gy, gm, gd] = String(iso).slice(0, 10).split("-").map(Number);
  if (!gy || !gm || !gd) return "";
  const [jy, jm, jd] = gregorianToJalali(gy, gm, gd);
  return `${jy}/${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")}`;
}

// "1405/5/18" (Persian or latin digits, / or -) -> "2026-08-09", null if invalid
export function jalaliToIso(str) {
  const normalized = String(str || "")
    .trim()
    .replace(/[۰-۹]/g, (d) => "0123456789"["۰۱۲۳۴۵۶۷۸۹".indexOf(d)]);
  const m = normalized.match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$/);
  if (!m) return null;
  const jy = +m[1], jm = +m[2], jd = +m[3];
  if (jm < 1 || jm > 12 || jd < 1 || jd > 31) return null;
  const [gy, gm, gd] = jalaliToGregorian(jy, jm, jd);
  return `${gy}-${String(gm).padStart(2, "0")}-${String(gd).padStart(2, "0")}`;
}
