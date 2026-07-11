/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
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
