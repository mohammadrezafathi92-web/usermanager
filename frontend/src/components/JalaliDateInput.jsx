import React, { useEffect, useState } from "react";
import { Calendar, ChevronRight, ChevronLeft } from "lucide-react";
import { isoToJalali, jalaliToIso } from "../utils.js";

// In Persian mode the range filters accept a TYPED Jalali date (e.g.
// ۱۴۰۵/۰۵/۱۸ or 1405/05/18 - Persian digits, / or - all fine) AND offer a
// click-to-pick Jalali calendar popover next to the input; either way the
// value held in state is ALWAYS the ISO/Gregorian string the API speaks
// (see utils.js's jalaliToIso). English mode keeps the native browser
// date picker.
const JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"];
const JALALI_WEEKDAYS = ["ش", "ی", "د", "س", "چ", "پ", "ج"]; // Persian week: شنبه..جمعه

const faDigits = (n) => String(n).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[d]);

// Esfand has 30 days only in a leap year - detected by round-tripping
// 12/30 through the Gregorian conversion (a non-leap 12/30 lands on a
// different date coming back) instead of duplicating the leap rule here.
const jalaliDaysInMonth = (jy, jm) => {
  if (jm <= 6) return 31;
  if (jm <= 11) return 30;
  return isoToJalali(jalaliToIso(`${jy}/12/30`)) === `${jy}/12/30` ? 30 : 29;
};

function JalaliCalendar({ value, onPick }) {
  const today = isoToJalali(new Date().toISOString());
  const start = (value ? isoToJalali(value) : today).split("/").map(Number);
  const [jy, setJy] = useState(start[0]);
  const [jm, setJm] = useState(start[1]);
  const selected = value ? isoToJalali(value) : null;

  const days = jalaliDaysInMonth(jy, jm);
  // Column of the month's 1st in a Saturday-first week: JS getDay() has
  // Sun=0..Sat=6, so Sat maps to 0 via (d+1)%7.
  const firstIso = jalaliToIso(`${jy}/${jm}/1`);
  const offset = (new Date(`${firstIso}T12:00:00`).getDay() + 1) % 7;
  const yearOptions = [];
  for (let y = start[0] - 8; y <= start[0] + 3; y++) yearOptions.push(y);

  const prevMonth = () => (jm === 1 ? (setJm(12), setJy(jy - 1)) : setJm(jm - 1));
  const nextMonth = () => (jm === 12 ? (setJm(1), setJy(jy + 1)) : setJm(jm + 1));

  return (
    <div className="absolute z-50 mt-1 right-0 w-64 bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-700 rounded-xl shadow-lg p-3" dir="rtl">
      <div className="flex items-center justify-between gap-1 mb-2">
        <button type="button" className="p-1 text-gray-400 hover:text-gray-600" onClick={prevMonth}><ChevronRight size={16} /></button>
        <select className="input-sm flex-1" value={jm} onChange={(e) => setJm(Number(e.target.value))}>
          {JALALI_MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
        </select>
        <select className="input-sm w-20" value={jy} onChange={(e) => setJy(Number(e.target.value))}>
          {yearOptions.map((y) => <option key={y} value={y}>{faDigits(y)}</option>)}
        </select>
        <button type="button" className="p-1 text-gray-400 hover:text-gray-600" onClick={nextMonth}><ChevronLeft size={16} /></button>
      </div>
      <div className="grid grid-cols-7 gap-0.5 text-center text-[11px] text-gray-400 mb-1">
        {JALALI_WEEKDAYS.map((w) => <div key={w}>{w}</div>)}
      </div>
      <div className="grid grid-cols-7 gap-0.5 text-center text-xs">
        {Array.from({ length: offset }).map((_, i) => <div key={`b${i}`} />)}
        {Array.from({ length: days }).map((_, i) => {
          const d = i + 1;
          const key = `${jy}/${String(jm).padStart(2, "0")}/${String(d).padStart(2, "0")}`;
          const isSelected = selected === key;
          const isToday = today === key;
          return (
            <button
              key={d}
              type="button"
              onClick={() => onPick(jalaliToIso(key))}
              className={`rounded-lg py-1 transition-colors ${
                isSelected
                  ? "bg-brand-600 text-white"
                  : isToday
                    ? "bg-brand-50 text-brand-700 dark:bg-brand-500/10 dark:text-brand-400 font-bold"
                    : "text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-slate-800"
              }`}
            >
              {faDigits(d)}
            </button>
          );
        })}
      </div>
      <button
        type="button"
        className="w-full mt-2 text-xs text-brand-600 hover:text-brand-700"
        onClick={() => onPick(jalaliToIso(today))}
      >
        امروز
      </button>
    </div>
  );
}

export function JalaliDateInput({ value, onChange, lang }) {
  const [text, setText] = useState(value ? isoToJalali(value) : "");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    setText(value ? isoToJalali(value) : "");
  }, [value]);
  if (lang === "en") {
    return <input type="date" className="input" value={value} onChange={(e) => onChange(e.target.value)} />;
  }
  const valid = !text.trim() || jalaliToIso(text) !== null;
  return (
    <div className="relative inline-block">
      <div className="flex items-center gap-1">
        <input
          type="text"
          dir="ltr"
          className={`input w-32 text-center ${valid ? "" : "border-red-400 focus:border-red-400"}`}
          placeholder="۱۴۰۵/۰۵/۱۸"
          value={text}
          onChange={(e) => {
            const v = e.target.value;
            setText(v);
            if (!v.trim()) {
              onChange("");
              return;
            }
            const iso = jalaliToIso(v);
            if (iso) onChange(iso);
          }}
        />
        <button
          type="button"
          className="p-2 rounded-xl text-gray-400 hover:text-brand-600 hover:bg-gray-50 dark:hover:bg-slate-800 transition-colors"
          onClick={() => setOpen(!open)}
        >
          <Calendar size={17} />
        </button>
      </div>
      {open && (
        <>
          {/* click-away backdrop */}
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <JalaliCalendar
            value={value}
            onPick={(iso) => {
              onChange(iso);
              setOpen(false);
            }}
          />
        </>
      )}
    </div>
  );
}

function DateFilters({ dateFrom, dateTo, setDateFrom, setDateTo, t, lang, children }) {
  return (
    <div className="flex flex-wrap items-end gap-3 mb-4">
      <div>
        <label className="block text-xs text-gray-400 mb-1">{t("accounting.filterFrom")}</label>
        <JalaliDateInput value={dateFrom} onChange={setDateFrom} lang={lang} />
      </div>
      <div>
        <label className="block text-xs text-gray-400 mb-1">{t("accounting.filterTo")}</label>
        <JalaliDateInput value={dateTo} onChange={setDateTo} lang={lang} />
      </div>
      {children}
    </div>
  );
}


export default JalaliDateInput;
