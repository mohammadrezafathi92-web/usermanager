import React, { useEffect, useState, useCallback } from "react";
import { Wallet, TrendingUp, TrendingDown, CreditCard, PiggyBank, Coins, Trash2, Download, Plus } from "lucide-react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from "recharts";
import Layout from "../components/Layout.jsx";
import MoneyInput from "../components/MoneyInput.jsx";
import JalaliDateInput from "../components/JalaliDateInput.jsx";
import Topbar from "../components/Topbar.jsx";
import StatCard from "../components/StatCard.jsx";
import {
  fetchAccountingSummary,
  fetchAccountingSeries,
  fetchAccountingSubtree,
  fetchAccountingTransactions,
  createAccountingExpense,
  deleteAccountingExpense,
  exportAccounting,
  fetchAdmins,
  topupAdminBalance,
} from "../api/client.js";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";
import { formatDateTime, isoToJalali, errorText } from "../utils.js";

// The «حساب‌داری» section - see backend routers/accounting.py +
// services/accounting.py. The backend already scopes everything by role
// (superadmin = whole panel, level-2 admin = own tree, seller = self), so
// this page only decides WHICH cards/blocks make sense to render per role
// (e.g. expenses/net-profit are superadmin-only concepts).
const fmt = (n) => (n === null || n === undefined ? "-" : Number(n).toLocaleString("en-US"));

function KindBadge({ kind, t }) {
  const tones = {
    sale_new: "bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400",
    sale_renew: "bg-brand-50 text-brand-700 dark:bg-brand-500/10 dark:text-brand-400",
    wallet_topup: "bg-sky-50 text-sky-600 dark:bg-sky-500/10 dark:text-sky-400",
    admin_credit_change: "bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400",
    admin_credit_spend: "bg-orange-50 text-orange-600 dark:bg-orange-500/10 dark:text-orange-400",
    admin_credit_refund: "bg-lime-50 text-lime-600 dark:bg-lime-500/10 dark:text-lime-400",
    expense: "bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400",
  };
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${tones[kind] || "bg-gray-100 text-gray-500"}`}>
      {t(`accounting.kind.${kind}`)}
    </span>
  );
}

function LoadFailed({ message, onRetry, t }) {
  return (
    <div className="card border border-red-200 bg-red-50">
      <div className="text-sm text-red-700 font-medium mb-1">{t("accounting.loadFailed")}</div>
      <div className="text-xs text-red-600 whitespace-pre-line mb-3">{message}</div>
      <button type="button" className="btn-secondary" onClick={onRetry}>{t("common.retry")}</button>
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

export default function Accounting() {
  const { t, language } = useLanguage();
  const { isSuperadmin, isAdminOrAbove, wallet } = useAuth();
  const [tab, setTab] = useState("dashboard");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  // Why a section failed to load, per section.
  //
  // Every loader here used to swallow its error into `set...(null)`, and
  // every section renders "loading" while its data is null - so ANY
  // failure (a 403, a timeout, a 500) left the page spinning forever with
  // nothing on screen and nothing in reach. Reported as "the accounting
  // section hangs", which was exactly true and told nobody anything.
  const [errors, setErrors] = useState({});
  const fail = (key) => (err) => setErrors((e) => ({ ...e, [key]: errorText(err, "خطا در بارگذاری") }));
  const clearError = (key) => setErrors((e) => ({ ...e, [key]: null }));

  // ---------------- dashboard ----------------
  const [summary, setSummary] = useState(null);
  const loadSummary = useCallback(() => {
    fetchAccountingSummary({ date_from: dateFrom || undefined, date_to: dateTo || undefined })
      .then((res) => { setSummary(res.data); clearError("dashboard"); })
      .catch((err) => { setSummary(null); fail("dashboard")(err); });
  }, [dateFrom, dateTo]);

  // ---------------- transactions ----------------
  const [txPage, setTxPage] = useState(1);
  const [txKind, setTxKind] = useState("");
  const [tx, setTx] = useState(null);
  const loadTx = useCallback(() => {
    fetchAccountingTransactions({
      page: txPage,
      page_size: 50,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      kind: txKind || undefined,
    })
      .then((res) => { setTx(res.data); clearError("transactions"); })
      .catch((err) => { setTx(null); fail("transactions")(err); });
  }, [txPage, txKind, dateFrom, dateTo]);

  // ---------------- expenses (superadmin) ----------------
  const [expenses, setExpenses] = useState(null);
  const [expForm, setExpForm] = useState({ amount: "", category: "", note: "", created_at: "" });
  const [expSaving, setExpSaving] = useState(false);
  const loadExpenses = useCallback(() => {
    fetchAccountingTransactions({ page: 1, page_size: 100, kind: "expense" })
      .then((res) => { setExpenses(res.data); clearError("expenses"); })
      .catch((err) => { setExpenses(null); fail("expenses")(err); });
  }, []);

  const submitExpense = async (e) => {
    e.preventDefault();
    // Number("-") is NaN, and an amount field that now accepts a leading minus
    // can hold exactly that mid-typing. The old `min="1"` guard went away with
    // type="number", so the check has to be explicit: an expense is always a
    // positive amount.
    const amount = Number(expForm.amount);
    if (!amount || amount <= 0) return;
    setExpSaving(true);
    try {
      await createAccountingExpense({
        amount,
        category: expForm.category || null,
        note: expForm.note || null,
        created_at: expForm.created_at ? `${expForm.created_at}T12:00:00` : null,
      });
      setExpForm({ amount: "", category: "", note: "", created_at: "" });
      loadExpenses();
      loadSummary();
    } finally {
      setExpSaving(false);
    }
  };

  const removeExpense = async (id) => {
    if (!window.confirm(t("accounting.deleteExpenseConfirm"))) return;
    await deleteAccountingExpense(id);
    loadExpenses();
    loadSummary();
  };

  // ---------------- admin credit (moved here from the ادمین‌ها page -
  // uses the same /admins/{id}/topup endpoint, whose _apply_balance_change
  // hook writes the matching ledger row automatically) ----------------
  const [admins, setAdmins] = useState(null);
  const [creditForm, setCreditForm] = useState({}); // { [adminId]: {amount, note} }
  const [creditSaving, setCreditSaving] = useState(null);
  const [creditError, setCreditError] = useState("");
  const loadAdmins = useCallback(() => {
    fetchAdmins()
      .then((res) => { setAdmins((res.data || []).filter((a) => !a.is_superadmin)); clearError("credit"); })
      .catch((err) => { setAdmins([]); fail("credit")(err); });
  }, []);

  const submitCredit = async (adminId) => {
    const f = creditForm[adminId] || {};
    const amount = Number(f.amount);
    if (!amount) return;
    setCreditSaving(adminId);
    setCreditError("");
    try {
      await topupAdminBalance(adminId, { amount, note: f.note || null });
      setCreditForm((c) => ({ ...c, [adminId]: { amount: "", note: "" } }));
      loadAdmins();
    } catch (err) {
      setCreditError(err?.response?.data?.detail || "خطا");
    } finally {
      setCreditSaving(null);
    }
  };

  // ---------------- subtree rollup ----------------
  // The aggregate half of "record vs aggregate visibility". Record access
  // was deliberately NOT taken away (an Admin funds their Sellers from
  // their own balance and is answerable for those sales); this is the view
  // that was missing - Sellers as accounts rather than as a merged customer
  // list.
  const [subtree, setSubtree] = useState(null);
  const loadSubtree = useCallback(() => {
    fetchAccountingSubtree({ date_from: dateFrom || undefined, date_to: dateTo || undefined })
      .then((res) => { setSubtree(res.data || []); clearError("subtree"); })
      .catch((err) => { setSubtree([]); fail("subtree")(err); });
  }, [dateFrom, dateTo]);

  // ---------------- reports ----------------
  const [granularity, setGranularity] = useState("day");
  const [series, setSeries] = useState([]);
  const loadSeries = useCallback(() => {
    fetchAccountingSeries({ granularity, date_from: dateFrom || undefined, date_to: dateTo || undefined })
      .then((res) => { setSeries(res.data || []); clearError("reports"); })
      .catch((err) => { setSeries([]); fail("reports")(err); });
  }, [granularity, dateFrom, dateTo]);

  const doExport = async () => {
    const res = await exportAccounting({
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      kind: txKind || undefined,
    });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = "accounting.xlsx";
    a.click();
    URL.revokeObjectURL(url);
  };

  useEffect(() => {
    if (tab === "dashboard") loadSummary();
    else if (tab === "transactions") loadTx();
    else if (tab === "expenses") loadExpenses();
    else if (tab === "credit") loadAdmins();
    else if (tab === "subtree") loadSubtree();
    else if (tab === "reports") loadSeries();
  }, [tab, loadSummary, loadTx, loadExpenses, loadAdmins, loadSubtree, loadSeries]);

  const tabs = [
    { id: "dashboard", label: t("accounting.tabDashboard") },
    { id: "transactions", label: t("accounting.tabTransactions") },
    ...(isSuperadmin ? [{ id: "expenses", label: t("accounting.tabExpenses") }] : []),
    ...(isAdminOrAbove ? [{ id: "credit", label: t("accounting.tabCredit") }] : []),
    ...(isAdminOrAbove ? [{ id: "subtree", label: t("accounting.tabSubtree") }] : []),
    { id: "reports", label: t("accounting.tabReports") },
  ];

  const role = summary?.role;

  return (
    <Layout>
      <Topbar title={t("accounting.title")} subtitle={t("accounting.subtitle")} />

      <div className="flex gap-2 mb-6 flex-wrap">
        {tabs.map((x) => (
          <button
            key={x.id}
            type="button"
            onClick={() => setTab(x.id)}
            className={`rounded-xl px-4 py-2 text-sm font-medium transition-colors ${
              tab === x.id
                ? "bg-brand-600 text-white"
                : "bg-white text-gray-500 hover:bg-gray-50 dark:bg-slate-900 dark:text-gray-400 dark:hover:bg-slate-800"
            }`}
          >
            {x.label}
          </button>
        ))}
      </div>

      {/* ================= dashboard ================= */}
      {tab === "dashboard" && (
        <>
          <DateFilters dateFrom={dateFrom} dateTo={dateTo} setDateFrom={setDateFrom} setDateTo={setDateTo} t={t} lang={language}>
            <button type="button" className="btn-secondary" onClick={loadSummary}>{t("accounting.apply")}</button>
          </DateFilters>

          {!summary ? (
            errors.dashboard ? <LoadFailed message={errors.dashboard} onRetry={loadSummary} t={t} /> : <div className="text-gray-400">{t("common.loading")}</div>
          ) : (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                <StatCard icon={TrendingUp} label={t("accounting.salesTotal")} value={`${fmt(summary.sales_total)}`} tone="emerald" />
                <StatCard icon={Coins} label={t("accounting.walletTopups")} value={`${fmt(summary.wallet_topup_total)}`} tone="brand" />
                {role === "superadmin" ? (
                  <>
                    <StatCard icon={TrendingDown} label={t("accounting.expensesTotal")} value={`${fmt(summary.expenses_total)}`} tone="red" />
                    <StatCard icon={PiggyBank} label={t("accounting.netProfit")} value={`${fmt(summary.net_profit)}`} tone={summary.net_profit >= 0 ? "emerald" : "red"} />
                  </>
                ) : (
                  <>
                    <StatCard icon={CreditCard} label={t("accounting.creditSpent")} value={`${fmt(summary.credit_spent_total)}`} tone="amber" />
                    <StatCard icon={Wallet} label={t("accounting.creditBalance")} value={`${fmt(summary.credit_balance)}`} tone="brand" />
                  </>
                )}
              </div>

              {role === "superadmin" && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-6">
                  <StatCard icon={CreditCard} label={t("accounting.cardCash")} value={`${fmt(summary.card_cash_total)}`} tone="brand" />
                  {summary.margin_total !== undefined && (
                    <StatCard icon={PiggyBank} label={t("accounting.margin")} value={`${fmt(summary.margin_total)}`} tone="emerald" />
                  )}
                </div>
              )}
              {role === "seller" && summary.margin_total !== null && summary.margin_total !== undefined && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-6">
                  <StatCard icon={PiggyBank} label={t("accounting.margin")} value={`${fmt(summary.margin_total)}`} tone="emerald" />
                </div>
              )}

              {summary.by_admin && summary.by_admin.length > 0 && (
                <div className="card mb-6 overflow-x-auto">
                  <h3 className="font-bold text-gray-700 dark:text-gray-200 mb-3">{t("accounting.byAdmin")}</h3>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-xs text-gray-400 border-b border-gray-50">
                        <th className="text-right font-medium px-4 py-2">{t("accounting.colAdmin")}</th>
                        <th className="text-right font-medium px-4 py-2">{t("accounting.colSales")}</th>
                        <th className="text-right font-medium px-4 py-2">{t("accounting.colCount")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.by_admin.map((row) => (
                        <tr key={row.admin_id ?? "self"} className="border-t border-gray-50">
                          <td className="px-4 py-2">{row.admin_username || t("accounting.mySales")}</td>
                          <td className="px-4 py-2 font-medium" dir="ltr">{fmt(row.sales_total)}</td>
                          <td className="px-4 py-2">{row.sales_count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {summary.by_card && summary.by_card.length > 0 && (
                <div className="card mb-6 overflow-x-auto">
                  <h3 className="font-bold text-gray-700 dark:text-gray-200 mb-3">{t("accounting.byCard")}</h3>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-xs text-gray-400 border-b border-gray-50">
                        <th className="text-right font-medium px-4 py-2">{t("accounting.colCard")}</th>
                        <th className="text-right font-medium px-4 py-2">{t("accounting.colTotal")}</th>
                        <th className="text-right font-medium px-4 py-2">{t("accounting.colCount")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.by_card.map((row) => (
                        <tr key={row.payment_card_id} className="border-t border-gray-50">
                          <td className="px-4 py-2 font-mono" dir="ltr">
                            {row.card_number || `#${row.payment_card_id}`}
                            {row.card_holder ? ` (${row.card_holder})` : ""}
                          </td>
                          <td className="px-4 py-2 font-medium" dir="ltr">{fmt(row.total)}</td>
                          <td className="px-4 py-2">{row.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </>
      )}

      {/* ================= transactions ================= */}
      {tab === "transactions" && (
        <>
          <DateFilters dateFrom={dateFrom} dateTo={dateTo} setDateFrom={setDateFrom} setDateTo={setDateTo} t={t} lang={language}>
            <div>
              <label className="block text-xs text-gray-400 mb-1">{t("accounting.filterKind")}</label>
              <select className="input" value={txKind} onChange={(e) => { setTxKind(e.target.value); setTxPage(1); }}>
                <option value="">{t("accounting.allKinds")}</option>
                {["sale_new", "sale_renew", "wallet_topup", "admin_credit_change", "admin_credit_spend", "admin_credit_refund", ...(isSuperadmin ? ["expense"] : [])].map((k) => (
                  <option key={k} value={k}>{t(`accounting.kind.${k}`)}</option>
                ))}
              </select>
            </div>
            <button type="button" className="btn-secondary" onClick={loadTx}>{t("accounting.apply")}</button>
            <button type="button" className="btn-secondary flex items-center gap-1" onClick={doExport}>
              <Download size={15} /> {t("accounting.export")}
            </button>
          </DateFilters>

          {!tx ? (
            errors.transactions ? <LoadFailed message={errors.transactions} onRetry={loadTx} t={t} /> : <div className="text-gray-400">{t("common.loading")}</div>
          ) : tx.items.length === 0 ? (
            <div className="card text-gray-400">{t("accounting.noData")}</div>
          ) : (
            <div className="card overflow-x-auto p-0">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-400 border-b border-gray-50">
                    <th className="text-right font-medium px-4 py-3">{t("accounting.kind")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.amount")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.customer")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.colAdmin")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.package")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.method")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.date")}</th>
                  </tr>
                </thead>
                <tbody>
                  {tx.items.map((e) => (
                    <tr key={e.id} className="border-t border-gray-50 hover:bg-gray-50/60 dark:hover:bg-slate-800/40">
                      <td className="px-4 py-3"><KindBadge kind={e.kind} t={t} /></td>
                      <td className="px-4 py-3 font-medium" dir="ltr">{fmt(e.amount)}</td>
                      <td className="px-4 py-3">{e.username_snapshot || "-"}</td>
                      <td className="px-4 py-3">{e.admin_username_snapshot || "-"}</td>
                      <td className="px-4 py-3">{e.package_name_snapshot || e.category || "-"}</td>
                      <td className="px-4 py-3">{e.payment_method ? t(`accounting.method.${e.payment_method}`) : "-"}</td>
                      <td className="px-4 py-3 text-gray-400 text-xs" dir="ltr">{formatDateTime(e.created_at, language)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="flex items-center justify-between px-4 py-3 text-sm text-gray-400 border-t border-gray-50">
                <span>{tx.total.toLocaleString("en-US")}</span>
                <div className="flex gap-2">
                  <button type="button" className="btn-secondary" disabled={txPage <= 1} onClick={() => { setTxPage(txPage - 1); }}>‹</button>
                  <span className="py-2">{txPage}</span>
                  <button type="button" className="btn-secondary" disabled={txPage * 50 >= tx.total} onClick={() => { setTxPage(txPage + 1); }}>›</button>
                </div>
              </div>
            </div>
          )}
        </>
      )}

      {/* ================= expenses (superadmin only) ================= */}
      {tab === "expenses" && isSuperadmin && (
        <>
          <form onSubmit={submitExpense} className="card mb-6 flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-xs text-gray-400 mb-1">{t("accounting.expenseAmount")}</label>
              <MoneyInput
                required
                value={expForm.amount}
                onChange={(v) => setExpForm({ ...expForm, amount: v })}
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">{t("accounting.expenseCategory")}</label>
              <input type="text" className="input" value={expForm.category} onChange={(e) => setExpForm({ ...expForm, category: e.target.value })} />
            </div>
            <div className="flex-1 min-w-[180px]">
              <label className="block text-xs text-gray-400 mb-1">{t("accounting.expenseNote")}</label>
              <input type="text" className="input w-full" value={expForm.note} onChange={(e) => setExpForm({ ...expForm, note: e.target.value })} />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">{t("accounting.expenseDate")}</label>
              <JalaliDateInput value={expForm.created_at} onChange={(v) => setExpForm({ ...expForm, created_at: v })} lang={language} />
            </div>
            <button type="submit" disabled={expSaving} className="btn-primary flex items-center gap-1">
              <Plus size={15} /> {t("accounting.addExpense")}
            </button>
          </form>

          {!expenses ? (
            errors.expenses ? <LoadFailed message={errors.expenses} onRetry={loadExpenses} t={t} /> : <div className="text-gray-400">{t("common.loading")}</div>
          ) : expenses.items.length === 0 ? (
            <div className="card text-gray-400">{t("accounting.noData")}</div>
          ) : (
            <div className="card overflow-x-auto p-0">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-400 border-b border-gray-50">
                    <th className="text-right font-medium px-4 py-3">{t("accounting.amount")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.category")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.note")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.date")}</th>
                    <th className="text-right font-medium px-4 py-3"></th>
                  </tr>
                </thead>
                <tbody>
                  {expenses.items.map((e) => (
                    <tr key={e.id} className="border-t border-gray-50">
                      <td className="px-4 py-3 font-medium" dir="ltr">{fmt(e.amount)}</td>
                      <td className="px-4 py-3">{e.category || "-"}</td>
                      <td className="px-4 py-3 text-gray-500">{e.note || "-"}</td>
                      <td className="px-4 py-3 text-gray-400 text-xs" dir="ltr">{language === "en" ? (e.created_at || "").slice(0, 10) : isoToJalali(e.created_at)}</td>
                      <td className="px-4 py-3">
                        <button type="button" className="text-red-500 hover:text-red-700" onClick={() => removeExpense(e.id)} title={t("accounting.deleteExpense")}>
                          <Trash2 size={16} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {/* ================= subtree rollup (superadmin + level-2) ================= */}
      {tab === "subtree" && isAdminOrAbove && (
        <>
          <DateFilters
            dateFrom={dateFrom} dateTo={dateTo}
            setDateFrom={setDateFrom} setDateTo={setDateTo}
            t={t} lang={language}
          />
          <div className="text-xs text-gray-400 mb-3">{t("accounting.subtreeHint")}</div>
          {!subtree ? (
            errors.credit ? <LoadFailed message={errors.credit} onRetry={loadAdmins} t={t} /> : <div className="text-gray-400">{t("common.loading")}</div>
          ) : subtree.length === 0 ? (
            <div className="card text-gray-400">{t("accounting.subtreeEmpty")}</div>
          ) : (
            <>
              {/* Totals first: the reason to open this tab is usually "how
                  is the whole group doing", and making that the first thing
                  read saves adding a column up by eye. */}
              <div className="stat-grid mb-4">
                <StatCard
                  icon={<TrendingUp size={18} />}
                  label={t("accounting.subtreeTotalSales")}
                  value={fmt(subtree.reduce((a, r) => a + (r.sales_total || 0), 0))}
                />
                <StatCard
                  icon={<Wallet size={18} />}
                  label={t("accounting.subtreeTotalCustomers")}
                  value={fmt(subtree.reduce((a, r) => a + (r.customers || 0), 0))}
                />
                <StatCard
                  icon={<Coins size={18} />}
                  label={t("accounting.subtreeTotalCredit")}
                  value={fmt(subtree.reduce((a, r) => a + (r.balance || 0), 0))}
                />
                <StatCard
                  icon={<TrendingDown size={18} />}
                  label={t("accounting.subtreeInDebt")}
                  value={fmt(subtree.filter((r) => r.in_debt).length)}
                />
              </div>

              <div className="card overflow-x-auto p-0">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-gray-400 border-b border-gray-50">
                      <th className="text-right font-medium px-4 py-3">{t("accounting.colAdmin")}</th>
                      <th className="text-right font-medium px-4 py-3">{t("accounting.subtreeCustomers")}</th>
                      <th className="text-right font-medium px-4 py-3">{t("accounting.subtreeSales")}</th>
                      <th className="text-right font-medium px-4 py-3">{t("accounting.creditBalance")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {subtree.map((r) => (
                      <tr key={r.id} className="border-b border-gray-50 last:border-0">
                        <td className="px-4 py-3">
                          <div className="font-medium text-gray-700">{r.username}</div>
                          <div className="text-xs text-gray-400">
                            {r.role === "admin" ? t("admins.roleAdmin") : t("admins.roleSellerPlain")}
                          </div>
                        </td>
                        <td className="px-4 py-3 tabular-nums" dir="ltr">
                          {fmt(r.customers)}
                          {/* Active is the number that says whether those
                              customers are still worth anything. */}
                          <span className="text-xs text-gray-400"> ({fmt(r.active_customers)})</span>
                        </td>
                        <td className="px-4 py-3 tabular-nums" dir="ltr">
                          {fmt(r.sales_total)}
                          <span className="text-xs text-gray-400"> ({fmt(r.sales_count)})</span>
                        </td>
                        <td className="px-4 py-3 tabular-nums" dir="ltr">
                          <span className={r.in_debt ? "text-red-500 font-medium" : "text-gray-700"}>
                            {fmt(r.balance)}
                          </span>
                          {r.credit_limit > 0 && (
                            <span className="text-xs text-gray-400"> / -{fmt(r.credit_limit)}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}

      {/* ================= admin credit (superadmin + level-2) ================= */}
      {tab === "credit" && isAdminOrAbove && (
        <>
          {/* A superadmin CREATES credit here; anyone else MOVES their own.
              Saying which, with the number, before they type an amount -
              otherwise the first sign of the limit is a refusal after the
              fact. */}
          {!isSuperadmin && (
            <div className="card mb-3 flex items-center justify-between gap-3 flex-wrap">
              <span className="text-sm text-gray-600">{t("accounting.creditTransferNote")}</span>
              <span className="font-bold text-gray-800 tabular-nums" dir="ltr">
                {fmt(wallet?.balance || 0)}
              </span>
            </div>
          )}
          {creditError && <div className="text-sm text-red-500 mb-3">{creditError}</div>}
          {!admins ? (
            errors.reports ? <LoadFailed message={errors.reports} onRetry={loadSeries} t={t} /> : <div className="text-gray-400">{t("common.loading")}</div>
          ) : admins.length === 0 ? (
            <div className="card text-gray-400">{t("accounting.noAdmins")}</div>
          ) : (
            <div className="card overflow-x-auto p-0">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-400 border-b border-gray-50">
                    <th className="text-right font-medium px-4 py-3">{t("accounting.colAdmin")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.creditBalance")}</th>
                    <th className="text-right font-medium px-4 py-3">{t("accounting.creditChange")}</th>
                  </tr>
                </thead>
                <tbody>
                  {admins.map((a) => {
                    const f = creditForm[a.id] || { amount: "", note: "" };
                    const usageMode = a.billing_mode === "usage";
                    return (
                      <tr key={a.id} className="border-t border-gray-50">
                        <td className="px-4 py-3">
                          <div className="font-medium text-gray-800 dark:text-gray-100">{a.username}</div>
                          {a.parent_admin_username && (
                            <div className="text-xs text-gray-400">{a.parent_admin_username}</div>
                          )}
                        </td>
                        <td className="px-4 py-3 font-medium" dir="ltr">
                          {usageMode
                            ? `${fmt(a.volume_balance_gb)} GB`
                            : `${fmt(a.balance)} ${t("accounting.toman")}`}
                        </td>
                        <td className="px-4 py-3">
                          {usageMode ? (
                            <span className="text-xs text-gray-400">{t("accounting.usageModeHint")}</span>
                          ) : (
                            <div className="flex flex-wrap gap-2 items-center">
                              {/* The one place a minus sign is meaningful: a
                                  negative amount deducts credit instead of
                                  adding it. */}
                              <MoneyInput
                                allowNegative
                                className="w-36"
                                placeholder={t("accounting.creditAmountPlaceholder")}
                                value={f.amount}
                                onChange={(v) => setCreditForm((c) => ({ ...c, [a.id]: { ...f, amount: v } }))}
                              />
                              <input
                                className="input w-44"
                                placeholder={t("accounting.expenseNote")}
                                value={f.note || ""}
                                onChange={(e) => setCreditForm((c) => ({ ...c, [a.id]: { ...f, note: e.target.value } }))}
                              />
                              <button
                                type="button"
                                className="btn-secondary shrink-0"
                                disabled={creditSaving === a.id || !Number(f.amount)}
                                onClick={() => submitCredit(a.id)}
                              >
                                {creditSaving === a.id ? "..." : t("accounting.apply")}
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div className="px-4 py-3 text-xs text-gray-400 border-t border-gray-50">{t("accounting.creditHint")}</div>
            </div>
          )}
        </>
      )}

      {/* ================= reports ================= */}
      {tab === "reports" && (
        <>
          <DateFilters dateFrom={dateFrom} dateTo={dateTo} setDateFrom={setDateFrom} setDateTo={setDateTo} t={t} lang={language}>
            <div>
              <label className="block text-xs text-gray-400 mb-1"> </label>
              <select className="input" value={granularity} onChange={(e) => setGranularity(e.target.value)}>
                <option value="day">{t("accounting.granularityDay")}</option>
                <option value="month">{t("accounting.granularityMonth")}</option>
              </select>
            </div>
            <button type="button" className="btn-secondary" onClick={loadSeries}>{t("accounting.apply")}</button>
            <button type="button" className="btn-secondary flex items-center gap-1" onClick={doExport}>
              <Download size={15} /> {t("accounting.export")}
            </button>
          </DateFilters>

          <div className="card" style={{ height: 340 }}>
            {series.length === 0 ? (
              <div className="text-gray-400">{t("accounting.noData")}</div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={language === "en" ? series : series.map((s) => ({ ...s, period: s.period.length === 7 ? isoToJalali(`${s.period}-15`).slice(0, 7) : isoToJalali(s.period) }))}>
                  <CartesianGrid strokeDasharray="3 3" strokeOpacity={0.15} />
                  <XAxis dataKey="period" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} tickFormatter={(v) => (v >= 1000000 ? `${v / 1000000}M` : v >= 1000 ? `${v / 1000}K` : v)} />
                  <Tooltip formatter={(v) => fmt(v)} />
                  <Legend />
                  <Bar dataKey="sales" name={t("accounting.chartSales")} fill="#10b981" radius={[4, 4, 0, 0]} />
                  {isSuperadmin && <Bar dataKey="expenses" name={t("accounting.chartExpenses")} fill="#ef4444" radius={[4, 4, 0, 0]} />}
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </>
      )}
    </Layout>
  );
}
