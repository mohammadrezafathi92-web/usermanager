/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      // Small phones are a real target here (sellers work from their phones),
      // and Tailwind's smallest built-in breakpoint is 640px - far too late
      // for deciding between a 1- and 2-column stat strip. `xs` covers the
      // 360-480px band where most of that decision actually happens.
      screens: {
        xs: "480px",
      },
      fontFamily: {
        sans: ["Vazirmatn", "system-ui", "sans-serif"],
        // Only used on the .tnum surfaces (tables, money, quotas) via the
        // `font-mono` utility where a page opts in - never a global switch,
        // Vazirmatn stays the body/UI font everywhere Persian text renders.
        mono: ["JetBrains Mono", "Vazirmatn", "ui-monospace", "monospace"],
      },
      // Indigo/violet in place of the old flat blue - same role (brand-500
      // is still the primary interactive color, brand-50/900 still the
      // tint/shade extremes), just a warmer, more premium hue. Every existing
      // bg-brand-*/text-brand-* usage across the panel picks this up for
      // free since nothing references the old hex values directly.
      colors: {
        brand: {
          50: "#eef2ff",
          100: "#e0e7ff",
          200: "#c7d2fe",
          300: "#a5b4fc",
          400: "#818cf8",
          500: "#6366f1",
          600: "#4f46e5",
          700: "#4338ca",
          800: "#3730a3",
          900: "#312e81",
        },
      },
      // Soft, colored shadows for the premium pass - a flat `shadow-sm` reads
      // cheap next to a gradient button/active nav pill. Kept as opt-in
      // tokens (not a default override) so nothing already using shadow-sm
      // changes shape unexpectedly.
      boxShadow: {
        glow: "0 8px 24px -6px rgba(99,102,241,0.45), 0 2px 8px -2px rgba(99,102,241,0.25)",
        "glow-sm": "0 4px 14px -4px rgba(99,102,241,0.4)",
        card: "0 1px 2px rgba(15,23,42,0.04), 0 8px 20px -12px rgba(15,23,42,0.10)",
        "card-dark": "0 0 0 1px rgba(148,163,184,0.06), 0 12px 28px -14px rgba(0,0,0,0.65)",
      },
    },
  },
  plugins: [],
};
