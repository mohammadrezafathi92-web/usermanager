import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App.jsx";
import { AuthProvider } from "./context/AuthContext.jsx";
import { LanguageProvider } from "./context/LanguageContext.jsx";
import "./index.css";

// جلوگیری از تغییر مقدار فیلدهای عددی با اسکرول ماوس: وقتی روی یک
// input[type=number] فوکوس است و کاربر اسکرول می‌کند، به‌جای تغییر عدد،
// فوکوس برداشته می‌شود تا اسکرول صفحه مثل حالت عادی کار کند.
document.addEventListener(
  "wheel",
  () => {
    const el = document.activeElement;
    if (el instanceof HTMLInputElement && el.type === "number") {
      el.blur();
    }
  },
  { passive: true }
);

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <LanguageProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </LanguageProvider>
    </BrowserRouter>
  </React.StrictMode>
);
