import React from "react";
import { ArrowUp, ArrowDown } from "lucide-react";

// نسخه‌ی select+دکمه‌ی سورت برای صفحات کارت/گرید (بدون جدول، پس بدون
// هدر ستون برای گذاشتن فلش کنارش - همون الگویی که Users.jsx از قبل
// برای انتخاب ستون سورت داشت، اینجا مستقل شده تا Nodes.jsx و Ads.jsx هم
// از همون استفاده کنن به‌جای این‌که هر کدوم نسخه‌ی خودشون رو بسازن).
export default function SortSelect({ value, dir, onChangeValue, onToggleDir, options, selectTitle, ascTitle, descTitle }) {
  return (
    <div className="flex items-center gap-1">
      <select
        className="input !w-auto min-w-[7rem] cursor-pointer"
        value={value}
        onChange={(e) => onChangeValue(e.target.value)}
        title={selectTitle}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <button type="button" className="btn-secondary btn-icon" title={dir === "asc" ? ascTitle : descTitle} onClick={onToggleDir}>
        {dir === "asc" ? <ArrowUp size={16} /> : <ArrowDown size={16} />}
      </button>
    </div>
  );
}
