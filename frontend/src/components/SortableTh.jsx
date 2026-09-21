import React from "react";
import { ArrowUp, ArrowDown, ArrowUpDown } from "lucide-react";

// یک <th> کلیک‌پذیر که فلش جهت سورت رو کنار عنوانش نشون می‌ده. هم برای
// سورت سمت سرور (Users.jsx - sortBy/sortDir استیت‌شون رو خود صفحه به بک‌اند
// می‌فرسته) و هم سورت سمت کلاینت (بقیه صفحات لیستی - روی آرایه‌ی فیلترشده
// اعمال می‌شه) قابل استفاده‌ست؛ خود کامپوننت فرقی بین این دو نمی‌ذاره، فقط
// state فعلی رو نشون می‌ده و کلیک رو به بالا پاس می‌ده.
export default function SortableTh({
  label,
  sortKey,
  sortBy,
  sortDir,
  onSort,
  className = "",
  align = "start",
}) {
  const active = sortBy === sortKey;
  const Icon = active ? (sortDir === "asc" ? ArrowUp : ArrowDown) : ArrowUpDown;
  return (
    <th className={className}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={`inline-flex items-center gap-1 hover:text-brand-600 dark:hover:text-brand-400 transition-colors ${
          align === "end" ? "flex-row-reverse" : ""
        }`}
      >
        <span>{label}</span>
        <Icon size={14} className={active ? "opacity-100" : "opacity-40"} />
      </button>
    </th>
  );
}
