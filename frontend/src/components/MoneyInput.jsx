import React from "react";
import { groupDigits, digitsOnly } from "../utils.js";

/**
 * The panel's single input for Toman amounts. Shows digits grouped
 * ("10,000,000") while storing and emitting plain digits ("10000000").
 *
 * It is `type="text"` rather than `type="number"` on purpose: a grouped value
 * is invalid in a number field, where the browser reports the whole input as
 * empty instead - the separators and a number input cannot coexist.
 * `inputMode="numeric"` keeps the numeric keypad on phones, and utils.js
 * normalises Persian/Arabic digits so a Persian keyboard works too.
 *
 * `onChange` receives the cleaned STRING, not an event - call sites that keep
 * numbers in state convert once, at the point where they know their own shape.
 * `allowNegative` is off by default; only the admin-credit screen wants a
 * minus sign, where it means a deduction rather than a top-up.
 */
export default function MoneyInput({ value, onChange, allowNegative = false, small = false, className = "", ...rest }) {
  return (
    <input
      type="text"
      inputMode="numeric"
      dir="ltr"
      className={`${small ? "input-sm" : "input"} text-start ${className}`}
      value={groupDigits(value)}
      onChange={(e) => {
        const cleaned = digitsOnly(e.target.value);
        onChange(allowNegative ? cleaned : cleaned.replace("-", ""));
      }}
      {...rest}
    />
  );
}
