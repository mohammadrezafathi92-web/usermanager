export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "-";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const idx = Math.min(i, units.length - 1);
  const value = bytes / Math.pow(1024, idx);
  return `${value.toFixed(value >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`;
}

export function gbToBytes(gb) {
  return Math.round(Number(gb) * 1024 * 1024 * 1024);
}

export function bytesToGb(bytes) {
  if (!bytes) return 0;
  return +(bytes / (1024 * 1024 * 1024)).toFixed(2);
}

export const STATUS_LABELS = {
  active: "فعال",
  disabled: "غیرفعال",
  quota_exceeded: "اتمام حجم",
  expired: "منقضی‌شده",
};

export const STATUS_STYLES = {
  active: "bg-emerald-50 text-emerald-600",
  disabled: "bg-gray-100 text-gray-500",
  quota_exceeded: "bg-amber-50 text-amber-600",
  expired: "bg-red-50 text-red-600",
};

export function formatDate(value) {
  if (!value) return "بدون انقضا";
  const d = new Date(value);
  return d.toLocaleDateString("fa-IR", { year: "numeric", month: "2-digit", day: "2-digit" });
}

export function formatDateTime(value) {
  if (!value) return "-";
  const d = new Date(value);
  return d.toLocaleString("fa-IR");
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
