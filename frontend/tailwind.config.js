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
      },
      colors: {
        brand: {
          50: "#eef4ff",
          100: "#dfe8ff",
          200: "#c2d3ff",
          300: "#9bb4ff",
          400: "#6f8bff",
          500: "#4763f5",
          600: "#3546e0",
          700: "#2b37b8",
          800: "#252f92",
          900: "#232c73",
        },
      },
    },
  },
  plugins: [],
};
