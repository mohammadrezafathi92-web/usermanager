import React from "react";

export default function StatCard({ icon: Icon, label, value, tone = "brand", onClick }) {
  const tones = {
    brand: "bg-brand-50 text-brand-600",
    emerald: "bg-emerald-50 text-emerald-600",
    amber: "bg-amber-50 text-amber-600",
    red: "bg-red-50 text-red-600",
  };
  const Comp = onClick ? "button" : "div";
  return (
    <Comp
      type={onClick ? "button" : undefined}
      onClick={onClick}
      className={`card flex items-center gap-4 w-full text-right ${onClick ? "cursor-pointer hover:shadow-md hover:-translate-y-0.5 transition" : ""}`}
    >
      <div className={`w-12 h-12 rounded-2xl flex items-center justify-center ${tones[tone]}`}>
        <Icon size={22} />
      </div>
      <div>
        <div className="text-2xl font-bold text-gray-800 dark:text-gray-100">{value}</div>
        <div className="text-sm text-gray-400">{label}</div>
      </div>
    </Comp>
  );
}
