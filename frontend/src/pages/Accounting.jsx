import React, { useEffect, useState, useCallback, useRef } from "react";
import { Wallet, TrendingUp, TrendingDown, CreditCard, PiggyBank, Coins, Trash2, Download, Plus } from "lucide-react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from "recharts";
import Layout from "../components/Layout.jsx";
import Modal from "../components/Modal.jsx";
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
  fetchAccountingReceivables,
  createAccountingPayment,
  resetAccountingReceivables,
  exportAccounting,
  fetchAdmins,
  topupAdminBalance,
  topupAdminVolume,
  fetchAdminBalanceLogs,
  fetchAdminVolumeLogs,
  updateAdmin,
} from "../api/client.js";
import { useAuth } from "../context/AuthContext.jsx";
import { useLanguage } from "../context/LanguageContext.jsx";
import { formatDateTime, isoToJalali, errorText, formatToman, formatGb } from "../utils.js";

// The «حساب‌داری» section - see backend routers/accounting.py +
// services/accounting.py. The backend already scopes everything by role
// (superadmin = whole panel, level-2 admin = own tree, seller = self), so
// this page only decides WHICH cards/blocks make sense to render per role
// (e.g. expenses/net-profit are superadmin-only concepts).
//
// `fmt` used to always format with "en-US" regardless of the active
// language, unlike formatDate/formatToman elsewhere in the panel - moved
// inside the component below so it can follow `language` like everywhere
// else (found during the 2026-09 full-codebase audit).

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
  const fmt = (n) => formatToman(n, language);
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
  // Sequence guards below: without them, a slower earlier response (e.g.
  // right after changing the date range) could resolve AFTER a newer one
  // and overwrite it with stale data - same out-of-order-response bug
  // fixed on Users.jsx (found during the 2026-09 full-codebase audit).
  const summarySeq = useRef(0);
  const txSeq = useRef(0);
  const subtreeSeq = useRef(0);
  const [summary, setSummary] = useState(null);
  const loadSummary = useCallback(() => {
    const seq = ++summarySeq.current;
    fetchAccountingSummary({ date_from: dateFrom || undefined, date_to: dateTo || undefined })
      .then((res) => { if (seq !== summarySeq.current) return; setSummary(res.data); clearError("dashboard"); })
      .catch((err) => { if (seq !== summarySeq.current) return; setSummary(null); fail("dashboard")(err); });
  }, [dateFrom, dateTo]);

  // ---------------- transactions ----------------
  const [txPage, setTxPage] = useState(1);
  const [txKind, setTxKind] = useState("");
  const [tx, setTx] = useState(null);
  const loadTx = useCallback(() => {
    const seq = ++txSeq.current;
    fetchAccountingTransactions({
      page: txPage,
      page_size: 50,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      kind: txKind || undefined,
    })
      .then((res) => { if (seq !== txSeq.current) return; setTx(res.data); clearError("transactions"); })
      .catch((err) => { if (seq !== txSeq.current) return; setTx(null); fail("transactions")(err); });
  }, [txPage, txKind, dateFrom, dateTo]);

  // ---------------- receivables (طلب از نماینده‌ها) ----------------
  // Credit is routinely handed to a reseller before it is paid for, so
  // "what they were given" and "what they actually paid" are tracked
  // separately - this tab is where the payment gets recorded.
  const receivablesSeq = useRef(0);
  const [receivables, setReceivables] = useState(null);
  const [payFor, setPayFor] = useState(null);       // the row being settled
  const [payForm, setPayForm] = useState({ amount: "", note: "" });
  const [paySaving, setPaySaving] = useState(false);
  const [payError, setPayError] = useState("");
  const loadReceivables = useCallback(() => {
    const seq = ++receivablesSeq.current;
    fetchAccountingReceivables({ date_to: dateTo || undefined })
      .then((res) => { if (seq !== receivablesSeq.current) return; setReceivables(res.data); clearError("receivables"); })
      .catch((err) => { if (seq !== receivablesSeq.current) return; setReceivables(null); fail("receivables")(err); });
  }, [dateTo]);

  const [resetting, setResetting] = useState(false);
  const doResetReceivables = async () => {
    if (!window.confirm(t("accounting.resetConfirm"))) return;
    setResetting(true);
    try {
      await resetAccountingReceivables();
      loadReceivables();
      loadSummary();
    } catch (err) {
      fail("receivables")(err);
    } finally {
      setResetting(false);
    }
  };

  const openPayment = (row) => {
    setPayFor(row);
    // Pre-filled with the full outstanding amount, which is the common
    // case, but editable - partial payments are normal here.
    setPayForm({ amount: row.owed > 0 ? String(row.owed) : "", note: "" });
    setPayError("");
  };

  const submitPayment = async (e) => {
    e.preventDefault();
    if (!payFor) return;
    setPaySaving(true);
    setPayError("");
    try {
      await createAccountingPayment({
        admin_id: payFor.admin_id,
        amount: Number(payForm.amount),
        note: payForm.note || undefined,
      });
      setPayFor(null);
      loadReceivables();
      loadSummary();
    } catch (err) {
      setPayError(errorText(err, "خطا در ثبت دریافت"));
    } finally {
      setPaySaving(false);
    }
  };

  // ---------------- per-admin money settings ----------------
  // Everything about ONE reseller's money, in the section that owns money:
  // their GB pool (usage mode), overdraft limit and per-GB rate, plus the
  // top-up history. These used to be edited on the Admins page while the
  // ledger lived here, so a single reseller's finances were split across
  // two screens ("این نمیشه یه بخشی تو حسابداری باشه یه بخشی توی ادمین").
  const [moneyFor, setMoneyFor] = useState(null);
  const [moneyForm, setMoneyForm] = useState({ credit_limit: 0, wholesale_price_per_gb: 0 });
  const [moneySaving, setMoneySaving] = useState(false);
  const [moneyError, setMoneyError] = useState("");
  const [moneyMsg, setMoneyMsg] = useState("");
  const [volTopup, setVolTopup] = useState({ amount: "", note: "" });
  const [volSaving, setVolSaving] = useState(false);
  const [moneyLogs, setMoneyLogs] = useState(null);

  const openMoney = (a) => {
    setMoneyFor(a);
    setMoneyForm({
      credit_limit: a.credit_limit || 0,
      wholesale_price_per_gb: a.wholesale_price_per_gb || 0,
    });
    setVolTopup({ amount: "", note: "" });
    setMoneyError("");
    setMoneyMsg("");
    setMoneyLogs(null);
    const load = (a.billing_mode === "usage" ? fetchAdminVolumeLogs : fetchAdminBalanceLogs)(a.id);
    load.then((res) => setMoneyLogs(res.data)).catch(() => setMoneyLogs([]));
  };

  const saveMoneySettings = async () => {
    if (!moneyFor) return;
    setMoneySaving(true);
    setMoneyError("");
    setMoneyMsg("");
    try {
      await updateAdmin(moneyFor.id, {
        credit_limit: Number(moneyForm.credit_limit) || 0,
        wholesale_price_per_gb: Number(moneyForm.wholesale_price_per_gb) || 0,
      });
      setMoneyMsg(t("common.saved"));
      loadAdmins();
    } catch (err) {
      setMoneyError(errorText(err, "خطا در ذخیره"));
    } finally {
      setMoneySaving(false);
    }
  };

  const doVolumeTopup = async () => {
    if (!moneyFor) return;
    const amount = Number(volTopup.amount);
    if (!amount) return;
    setVolSaving(true);
    setMoneyError("");
    try {
      const res = await topupAdminVolume(moneyFor.id, { amount_gb: amount, note: volTopup.note || null });
      setMoneyFor((m) => ({ ...m, volume_balance_gb: res.data.volume_balance_gb }));
      setVolTopup({ amount: "", note: "" });
      loadAdmins();
      fetchAdminVolumeLogs(moneyFor.id).then((r) => setMoneyLogs(r.data)).catch(() => {});
    } catch (err) {
      setMoneyError(errorText(err, "خطا در شارژ حجم"));
    } finally {
      setVolSaving(false);
    }
  };

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
      // `paid` records the matching receipt in the same action. Left off,
      // the top-up stands as a receivable until the money is actually
      // collected - which is the normal case here (credit first, payment
      // later), so it deliberately defaults to off.
      await topupAdminBalance(adminId, { amount, note: f.note || null, paid: !!f.paid });
      setCreditForm((c) => ({ ...c, [adminId]: { amount: "", note: "", paid: false } }));
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
    const seq = ++subtreeSeq.current;
    fetchAccountingSubtree({ date_from: dateFrom || undefined, date_to: dateTo || undefined })
      .then((res) => { if (seq !== subtreeSeq.current) return; setSubtree(res.data || []); clearError("subtree"); })
      .catch((err) => { if (seq !== subtreeSeq.current) return; setSubtree([]); fail("subtree")(err); });
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
    else if (tab === "receivables") loadReceivables();
    else if (tab === "subtree") loadSubtree();
    else if (tab === "reports") loadSeries();
  }, [tab, loadSummary, loadTx, loadExpenses, loadAdmins, loadReceivables, loadSubtree, loadSeries]);

  const tabs = [
    { id: "dashboard", label: t("accounting.tabDashboard") },
    { id: "transactions", label: t("accounting.tabTransactions") },
    ...(isSuperadmin ? [{ id: "expenses", label: t("accounting.tabExpenses") }] : []),
    ...(isAdminOrAbove ? [{ id: "credit", label: t("accounting.tabCredit") }] : []),
    ...(isAdminOrAbove ? [{ id: "receivables", label: t("accounting.tabReceivables") }] : []),
    ...(isAdminOrAbove ? [{ id: "subtree", label: t("accounting.tabSubtree") }] : []),
    { id: "reports", label: t("accounting.tabReports") },
  ];

  const role = summary?.role;

  return (
    <Layout>
      <Topbar title={t("accounting.title")} subtitle={t("accounting.subtitle")} />

      <div className="flex items-center gap-1 border-b border-gray-100 dark:border-slate-800 mb-4 overflow-x-auto">
        {tabs.map((x) => (
          <button
            key={x.id}
            type="button"
            onClick={() => setTab(x.id)}
            className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
              tab === x.id
                ? "border-brand-600 text-brand-600"
                : "border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
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
              {role === "superadmin" ? (
                <>
                  {/* What is actually YOURS. A reseller's retail sale is the
                      reseller's money, so it is reported separately below
                      rather than added into one "فروش" figure. */}
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
                    <StatCard icon={TrendingUp} label={t("accounting.ownSales")} value={`${fmt(summary.own_sales_total)}`} tone="emerald" />
                    <StatCard icon={Wallet} label={t("accounting.paymentsReceived")} value={`${fmt(summary.admin_payments_total)}`} tone="brand" />
                    <StatCard icon={TrendingDown} label={t("accounting.expensesTotal")} value={`${fmt(summary.expenses_total)}`} tone="red" />
                    <StatCard icon={PiggyBank} label={t("accounting.netProfit")} value={`${fmt(summary.net_profit)}`} tone={summary.net_profit >= 0 ? "emerald" : "red"} />
                  </div>
                  <p className="hint mb-4">{t("accounting.netProfitHint")}</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                    <StatCard
                      icon={Coins}
                      label={t("accounting.receivables")}
                      value={`${fmt(summary.receivables_total)}`}
                      tone={summary.receivables_total > 0 ? "amber" : "emerald"}
                    />
                    <StatCard icon={TrendingUp} label={t("accounting.resellerTurnover")} value={`${fmt(summary.reseller_sales_total)}`} tone="brand" />
                    <StatCard icon={CreditCard} label={t("accounting.cardCash")} value={`${fmt(summary.card_cash_total)}`} tone="brand" />
                    <StatCard icon={Coins} label={t("accounting.walletTopups")} value={`${fmt(summary.wallet_topup_total)}`} tone="brand" />
                  </div>
                </>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                  <StatCard icon={TrendingUp} label={t("accounting.salesTotal")} value={`${fmt(summary.sales_total)}`} tone="emerald" />
                  <StatCard icon={CreditCard} label={t("accounting.creditSpent")} value={`${fmt(summary.credit_spent_total)}`} tone="amber" />
                  {/* Shown even when negative: spending credit and selling
                      nothing is a real loss, not an unknown. */}
                  {summary.margin_total !== null && summary.margin_total !== undefined && (
                    <StatCard
                      icon={PiggyBank}
                      label={t("accounting.margin")}
                      value={`${fmt(summary.margin_total)}`}
                      tone={summary.margin_total >= 0 ? "emerald" : "red"}
                    />
                  )}
                  {/* A usage-billed account holds GB, not tomans. */}
                  {summary.billing_mode === "usage" ? (
                    <StatCard icon={Wallet} label={t("accounting.volumeBalance")} value={`${formatGb(summary.volume_balance_gb)} GB`} tone="brand" />
                  ) : (
                    <StatCard icon={Wallet} label={t("accounting.creditBalance")} value={`${fmt(summary.credit_balance)}`} tone="brand" />
                  )}
                </div>
              )}

              {role !== "superadmin" && !!summary.owed_total && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-6">
                  <StatCard
                    icon={Coins}
                    label={t("accounting.owedByMe")}
                    value={`${fmt(summary.owed_total)}`}
                    tone={summary.owed_total > 0 ? "amber" : "emerald"}
                  />
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
                          <td className="px-4 py-2 font-medium text-right" dir="ltr">{fmt(row.sales_total)}</td>
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
                          <td className="px-4 py-2 font-mono text-right" dir="ltr">
                            {row.card_number || `#${row.payment_card_id}`}
                            {row.card_holder ? ` (${row.card_holder})` : ""}
                          </td>
                          <td className="px-4 py-2 font-medium text-right" dir="ltr">{fmt(row.total)}</td>
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
          <DateFilters
            dateFrom={dateFrom}
            dateTo={dateTo}
            // Unlike the kind filter's own onChange just below (which
            // already resets txPage), changing either date used to leave
            // txPage wherever it was - an admin on page 3 who narrowed the
            // range to a period with only a couple of rows saw an empty
            // table even though page 1 had data (found during the 2026-09
            // full-codebase audit).
            setDateFrom={(v) => { setDateFrom(v); setTxPage(1); }}
            setDateTo={(v) => { setDateTo(v); setTxPage(1); }}
            t={t}
            lang={language}
          >
            <div>
              <label className="block text-xs text-gray-400 mb-1">{t("accounting.filterKind")}</label>
              <select className="input" value={txKind} onChange={(e) => { setTxKind(e.target.value); setTxPage(1); }}>
                <option value="">{t("accounting.allKinds")}</option>
                {["sale_new", "sale_renew", "wallet_topup", "admin_credit_change", "admin_credit_spend", "admin_credit_refund", "admin_usage_charge", "admin_payment", ...(isSuperadmin ? ["expense"] : [])].map((k) => (
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
            <div className="card p-0">
              {/* دسکتاپ: جدول - از md به بالا نمایش داده می‌شود */}
              <div className="hidden md:block overflow-x-auto">
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
                        <td className="px-4 py-3 font-medium text-right" dir="ltr">{fmt(e.amount)}</td>
                        <td className="px-4 py-3">{e.username_snapshot || "-"}</td>
                        <td className="px-4 py-3">{e.admin_username_snapshot || "-"}</td>
                        <td className="px-4 py-3">{e.package_name_snapshot || e.category || "-"}</td>
                        <td className="px-4 py-3">{e.payment_method ? t(`accounting.method.${e.payment_method}`) : "-"}</td>
                        <td className="px-4 py-3 text-gray-400 text-xs text-right" dir="ltr">{formatDateTime(e.created_at, language)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* موبایل: کارت - زیر md نمایش داده می‌شود */}
              <div className="md:hidden divide-y divide-gray-50">
                {tx.items.map((e) => (
                  <div key={e.id} className="p-4">
                    <div className="flex items-center justify-between gap-2">
                      <KindBadge kind={e.kind} t={t} />
                      <span className="font-medium" dir="ltr">{fmt(e.amount)}</span>
                    </div>
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-xs text-gray-500">
                      {e.username_snapshot && <span>{e.username_snapshot}</span>}
                      {e.admin_username_snapshot && <span>{e.admin_username_snapshot}</span>}
                      {(e.package_name_snapshot || e.category) && <span>{e.package_name_snapshot || e.category}</span>}
                      {e.payment_method && <span>{t(`accounting.method.${e.payment_method}`)}</span>}
                    </div>
                    <div className="text-gray-400 text-xs mt-1" dir="ltr">{formatDateTime(e.created_at, language)}</div>
                  </div>
                ))}
              </div>

              <div className="flex items-center justify-between px-4 py-3 text-sm text-gray-400 border-t border-gray-50">
                <span>{fmt(tx.total)}</span>
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
            <div className="card p-0">
              {/* دسکتاپ: جدول - از md به بالا نمایش داده می‌شود */}
              <div className="hidden md:block overflow-x-auto">
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
                        <td className="px-4 py-3 font-medium text-right" dir="ltr">{fmt(e.amount)}</td>
                        <td className="px-4 py-3">{e.category || "-"}</td>
                        <td className="px-4 py-3 text-gray-500">{e.note || "-"}</td>
                        <td className="px-4 py-3 text-gray-400 text-xs text-right" dir="ltr">{language === "en" ? (e.created_at || "").slice(0, 10) : isoToJalali(e.created_at)}</td>
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

              {/* موبایل: کارت - زیر md نمایش داده می‌شود */}
              <div className="md:hidden divide-y divide-gray-50">
                {expenses.items.map((e) => (
                  <div key={e.id} className="p-4">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium" dir="ltr">{fmt(e.amount)}</span>
                      <button type="button" className="text-red-500 hover:text-red-700" onClick={() => removeExpense(e.id)} title={t("accounting.deleteExpense")}>
                        <Trash2 size={16} />
                      </button>
                    </div>
                    <div className="text-sm text-gray-600 mt-1">{e.category || "-"}</div>
                    {e.note && <div className="text-xs text-gray-500 mt-1">{e.note}</div>}
                    <div className="text-gray-400 text-xs mt-1" dir="ltr">
                      {language === "en" ? (e.created_at || "").slice(0, 10) : isoToJalali(e.created_at)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {/* ================= receivables / طلب از نماینده‌ها ================= */}
      {tab === "receivables" && isAdminOrAbove && (
        <>
          <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
            <div>
              <p className="hint">{t("accounting.receivablesHint")}</p>
              {receivables?.start_at && (
                <p className="hint">
                  {t("accounting.countingSince", { value: formatDateTime(receivables.start_at, language) })}
                </p>
              )}
            </div>
            {isSuperadmin && (
              <button type="button" className="btn-secondary shrink-0" disabled={resetting} onClick={doResetReceivables}>
                {resetting ? "..." : t("accounting.resetReceivables")}
              </button>
            )}
          </div>
          {!receivables ? (
            errors.receivables ? <LoadFailed message={errors.receivables} onRetry={loadReceivables} t={t} /> : <div className="text-gray-400">{t("common.loading")}</div>
          ) : receivables.items.length === 0 ? (
            <div className="card text-gray-400">{t("accounting.noReceivables")}</div>
          ) : (
            <>
              <div className="stat-grid mb-4">
                <StatCard
                  icon={Coins}
                  label={t("accounting.receivables")}
                  value={fmt(receivables.total)}
                />
              </div>

              <div className="card p-0">
                {/* دسکتاپ: جدول - از md به بالا نمایش داده می‌شود */}
                <div className="hidden md:block overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-xs text-gray-400 border-b border-gray-50">
                        <th className="text-right font-medium px-4 py-3">{t("accounting.colAdmin")}</th>
                        <th className="text-right font-medium px-4 py-3">{t("accounting.charged")}</th>
                        <th className="text-right font-medium px-4 py-3">{t("accounting.paid")}</th>
                        <th className="text-right font-medium px-4 py-3">{t("accounting.owed")}</th>
                        <th className="text-right font-medium px-4 py-3"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {receivables.items.map((r) => (
                        <tr key={r.admin_id} className="border-t border-gray-50">
                          <td className="px-4 py-3">
                            <div className="font-medium text-gray-700 dark:text-gray-200">{r.username}</div>
                            {r.deleted && <div className="text-xs text-gray-400">{t("accounting.deletedAdmin")}</div>}
                          </td>
                          <td className="px-4 py-3 tabular-nums text-right" dir="ltr">{fmt(r.charged_total)}</td>
                          <td className="px-4 py-3 tabular-nums text-emerald-600 text-right" dir="ltr">{fmt(r.paid_total)}</td>
                          <td className="px-4 py-3 tabular-nums font-medium text-right" dir="ltr">
                            <span className={r.owed > 0 ? "text-red-500" : "text-gray-500"}>{fmt(r.owed)}</span>
                          </td>
                          <td className="px-4 py-3">
                            <button type="button" className="btn-secondary" onClick={() => openPayment(r)}>
                              <Plus size={14} /> {t("accounting.recordPayment")}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* موبایل: کارت - زیر md نمایش داده می‌شود */}
                <div className="md:hidden divide-y divide-gray-50">
                  {receivables.items.map((r) => (
                    <div key={r.admin_id} className="p-4">
                      <div className="flex items-center justify-between gap-2">
                        <div>
                          <div className="font-medium text-gray-700 dark:text-gray-200">{r.username}</div>
                          {r.deleted && <div className="text-xs text-gray-400">{t("accounting.deletedAdmin")}</div>}
                        </div>
                        <span className={`font-medium tabular-nums ${r.owed > 0 ? "text-red-500" : "text-gray-500"}`} dir="ltr">
                          {fmt(r.owed)}
                        </span>
                      </div>
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 text-xs text-gray-500">
                        <span>{t("accounting.charged")}: <span dir="ltr" className="tabular-nums">{fmt(r.charged_total)}</span></span>
                        <span className="text-emerald-600">{t("accounting.paid")}: <span dir="ltr" className="tabular-nums">{fmt(r.paid_total)}</span></span>
                      </div>
                      <button type="button" className="btn-secondary mt-3" onClick={() => openPayment(r)}>
                        <Plus size={14} /> {t("accounting.recordPayment")}
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}

          <Modal
            open={!!payFor}
            onClose={() => setPayFor(null)}
            title={t("accounting.recordPaymentFor", { name: payFor?.username || "" })}
          >
            <form onSubmit={submitPayment} className="space-y-4">
              <div>
                <label className="block text-sm text-gray-600 mb-1">{t("accounting.paymentAmount")}</label>
                <MoneyInput
                  autoFocus
                  value={payForm.amount}
                  onChange={(v) => setPayForm((f) => ({ ...f, amount: v }))}
                />
                <div className="hint">{t("accounting.paymentAmountHint", { value: fmt(payFor?.owed || 0) })}</div>
              </div>
              <div>
                <label className="block text-sm text-gray-600 mb-1">{t("accounting.expenseNote")}</label>
                <input className="input" value={payForm.note} onChange={(e) => setPayForm((f) => ({ ...f, note: e.target.value }))} />
              </div>
              {payError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{payError}</div>}
              <button type="submit" disabled={paySaving || !Number(payForm.amount)} className="btn-primary">
                {paySaving ? "..." : t("accounting.recordPayment")}
              </button>
            </form>
          </Modal>
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
                  icon={TrendingUp}
                  label={t("accounting.subtreeTotalSales")}
                  value={fmt(subtree.reduce((a, r) => a + (r.sales_total || 0), 0))}
                />
                <StatCard
                  icon={Wallet}
                  label={t("accounting.subtreeTotalCustomers")}
                  value={fmt(subtree.reduce((a, r) => a + (r.customers || 0), 0))}
                />
                {/* Whichever pool the accounts below actually hold. Summing
                    `balance` across everyone reported a healthy toman credit
                    for accounts that were switched to volume billing long
                    ago and hold only GB - the toman figure is a frozen
                    leftover (reported 2026-09). Each card only appears when
                    there is an account of that kind. */}
                {subtree.some((r) => r.billing_mode !== "usage") && (
                  <StatCard
                    icon={Coins}
                    label={t("accounting.subtreeTotalCredit")}
                    value={fmt(subtree.filter((r) => r.billing_mode !== "usage").reduce((a, r) => a + (r.balance || 0), 0))}
                  />
                )}
                {subtree.some((r) => r.billing_mode === "usage") && (
                  <StatCard
                    icon={Coins}
                    label={t("accounting.subtreeTotalVolume")}
                    value={`${formatGb(subtree.filter((r) => r.billing_mode === "usage").reduce((a, r) => a + (r.volume_balance_gb || 0), 0))} GB`}
                  />
                )}
                <StatCard
                  icon={TrendingDown}
                  label={t("accounting.subtreeInDebt")}
                  value={fmt(subtree.filter((r) => r.in_debt).length)}
                />
              </div>

              <div className="card p-0">
                {/* دسکتاپ: جدول - از md به بالا نمایش داده می‌شود */}
                <div className="hidden md:block overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-xs text-gray-400 border-b border-gray-50">
                        <th className="text-right font-medium px-4 py-3">{t("accounting.colAdmin")}</th>
                        <th className="text-right font-medium px-4 py-3">{t("accounting.subtreeCustomers")}</th>
                        <th className="text-right font-medium px-4 py-3">{t("accounting.subtreeSales")}</th>
                        <th className="text-right font-medium px-4 py-3">{t("accounting.owed")}</th>
                        <th className="text-right font-medium px-4 py-3">{t("accounting.subtreeBalanceCol")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {subtree.map((r) => (
                        <tr key={r.id} className="border-b border-gray-50 last:border-0">
                          <td className="px-4 py-3">
                            <div className="font-medium text-gray-700">{r.username}</div>
                            <div className="text-xs text-gray-400">
                              {r.role === "admin" ? t("admins.roleAdmin") : t("admins.roleSellerPlain")}
                              {r.sub_accounts > 0 && ` · ${t("accounting.subAccounts", { count: r.sub_accounts })}`}
                            </div>
                          </td>
                          <td className="px-4 py-3 tabular-nums text-right" dir="ltr">
                            {fmt(r.customers)}
                            {/* Active is the number that says whether those
                                customers are still worth anything. */}
                            <span className="text-xs text-gray-400"> ({fmt(r.active_customers)})</span>
                          </td>
                          <td className="px-4 py-3 tabular-nums text-right" dir="ltr">
                            {fmt(r.sales_total)}
                            <span className="text-xs text-gray-400"> ({fmt(r.sales_count)})</span>
                            {/* The branch total above includes this account's
                                sub-accounts; their own share is worth seeing
                                beside it, otherwise a rolled-up row can't be
                                told apart from one that sells everything
                                itself. */}
                            {r.sub_accounts > 0 && (
                              <div className="text-xs text-gray-400">
                                {t("accounting.ownShare", { value: fmt(r.own_sales_total) })}
                              </div>
                            )}
                          </td>
                          <td className="px-4 py-3 tabular-nums text-right" dir="ltr">
                            <span className={r.owed > 0 ? "text-red-500 font-medium" : "text-gray-500"}>{fmt(r.owed)}</span>
                          </td>
                          <td className="px-4 py-3 tabular-nums text-right" dir="ltr">
                            {/* A usage-billed account holds GB, not tomans. */}
                            {r.billing_mode === "usage" ? (
                              <span className={r.in_debt ? "text-red-500 font-medium" : "text-gray-700"}>
                                {formatGb(r.volume_balance_gb)} GB
                              </span>
                            ) : (
                              <>
                                <span className={r.in_debt ? "text-red-500 font-medium" : "text-gray-700"}>
                                  {fmt(r.balance)}
                                </span>
                                {r.credit_limit > 0 && (
                                  <span className="text-xs text-gray-400"> / -{fmt(r.credit_limit)}</span>
                                )}
                              </>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* موبایل: کارت - زیر md نمایش داده می‌شود */}
                <div className="md:hidden divide-y divide-gray-50">
                  {subtree.map((r) => (
                    <div key={r.id} className="p-4">
                      <div className="font-medium text-gray-700">{r.username}</div>
                      <div className="text-xs text-gray-400 mb-2">
                        {r.role === "admin" ? t("admins.roleAdmin") : t("admins.roleSellerPlain")}
                        {r.sub_accounts > 0 && ` · ${t("accounting.subAccounts", { count: r.sub_accounts })}`}
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-xs">
                        <div>
                          <div className="text-gray-400">{t("accounting.subtreeCustomers")}</div>
                          <div className="tabular-nums">{fmt(r.customers)} <span className="text-gray-400">({fmt(r.active_customers)})</span></div>
                        </div>
                        <div>
                          <div className="text-gray-400">{t("accounting.subtreeSales")}</div>
                          <div className="tabular-nums">{fmt(r.sales_total)} <span className="text-gray-400">({fmt(r.sales_count)})</span></div>
                          {r.sub_accounts > 0 && (
                            <div className="text-gray-400">{t("accounting.ownShare", { value: fmt(r.own_sales_total) })}</div>
                          )}
                        </div>
                        <div>
                          <div className="text-gray-400">{t("accounting.owed")}</div>
                          <div className="tabular-nums">
                            <span className={r.owed > 0 ? "text-red-500 font-medium" : "text-gray-700"}>{fmt(r.owed)}</span>
                          </div>
                        </div>
                        <div>
                          <div className="text-gray-400">
                            {r.billing_mode === "usage" ? t("accounting.volumeBalance") : t("accounting.creditBalance")}
                          </div>
                          <div className="tabular-nums">
                            {r.billing_mode === "usage" ? (
                              <span className={r.in_debt ? "text-red-500 font-medium" : "text-gray-700"}>
                                {formatGb(r.volume_balance_gb)} GB
                              </span>
                            ) : (
                              <>
                                <span className={r.in_debt ? "text-red-500 font-medium" : "text-gray-700"}>{fmt(r.balance)}</span>
                                {r.credit_limit > 0 && <span className="text-gray-400"> / -{fmt(r.credit_limit)}</span>}
                              </>
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
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
            <div className="card p-0">
              {/* دسکتاپ: جدول - از md به بالا نمایش داده می‌شود */}
              <div className="hidden md:block overflow-x-auto">
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
                          <td className="px-4 py-3 font-medium text-right" dir="ltr">
                            {usageMode
                              ? `${fmt(a.volume_balance_gb)} GB`
                              : `${fmt(a.balance)} ${t("accounting.toman")}`}
                          </td>
                          <td className="px-4 py-3">
                            {usageMode ? (
                              <button type="button" className="btn-secondary" onClick={() => openMoney(a)}>
                                <Wallet size={14} /> {t("accounting.moneySettings")}
                              </button>
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
                                <label className="flex items-center gap-1.5 text-xs text-gray-600 whitespace-nowrap">
                                  <input
                                    type="checkbox"
                                    checked={!!f.paid}
                                    onChange={(e) => setCreditForm((c) => ({ ...c, [a.id]: { ...f, paid: e.target.checked } }))}
                                  />
                                  {t("accounting.paidNow")}
                                </label>
                                <button
                                  type="button"
                                  className="btn-secondary shrink-0"
                                  disabled={creditSaving === a.id || !Number(f.amount)}
                                  onClick={() => submitCredit(a.id)}
                                >
                                  {creditSaving === a.id ? "..." : t("accounting.apply")}
                                </button>
                                <button type="button" className="btn-ghost shrink-0" title={t("accounting.moneySettings")} onClick={() => openMoney(a)}>
                                  <Wallet size={16} />
                                </button>
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {/* موبایل: کارت - زیر md نمایش داده می‌شود */}
              <div className="md:hidden divide-y divide-gray-50">
                {admins.map((a) => {
                  const f = creditForm[a.id] || { amount: "", note: "" };
                  const usageMode = a.billing_mode === "usage";
                  return (
                    <div key={a.id} className="p-4">
                      <div className="flex items-center justify-between gap-2">
                        <div>
                          <div className="font-medium text-gray-800 dark:text-gray-100">{a.username}</div>
                          {a.parent_admin_username && (
                            <div className="text-xs text-gray-400">{a.parent_admin_username}</div>
                          )}
                        </div>
                        <span className="font-medium text-sm" dir="ltr">
                          {usageMode ? `${fmt(a.volume_balance_gb)} GB` : `${fmt(a.balance)} ${t("accounting.toman")}`}
                        </span>
                      </div>
                      {usageMode ? (
                        <button type="button" className="btn-secondary mt-2" onClick={() => openMoney(a)}>
                          <Wallet size={14} /> {t("accounting.moneySettings")}
                        </button>
                      ) : (
                        <div className="flex flex-col gap-2 mt-3">
                          <MoneyInput
                            allowNegative
                            placeholder={t("accounting.creditAmountPlaceholder")}
                            value={f.amount}
                            onChange={(v) => setCreditForm((c) => ({ ...c, [a.id]: { ...f, amount: v } }))}
                          />
                          <input
                            className="input"
                            placeholder={t("accounting.expenseNote")}
                            value={f.note || ""}
                            onChange={(e) => setCreditForm((c) => ({ ...c, [a.id]: { ...f, note: e.target.value } }))}
                          />
                          <label className="flex items-center gap-1.5 text-xs text-gray-600">
                            <input
                              type="checkbox"
                              checked={!!f.paid}
                              onChange={(e) => setCreditForm((c) => ({ ...c, [a.id]: { ...f, paid: e.target.checked } }))}
                            />
                            {t("accounting.paidNow")}
                          </label>
                          <button
                            type="button"
                            className="btn-secondary"
                            disabled={creditSaving === a.id || !Number(f.amount)}
                            onClick={() => submitCredit(a.id)}
                          >
                            {creditSaving === a.id ? "..." : t("accounting.apply")}
                          </button>
                          <button type="button" className="btn-ghost" onClick={() => openMoney(a)}>
                            <Wallet size={14} /> {t("accounting.moneySettings")}
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              <div className="px-4 py-3 text-xs text-gray-400 border-t border-gray-50">{t("accounting.creditHint")}</div>
            </div>
          )}

          {/* One reseller's money, all of it, in the section that owns
              money - GB pool, overdraft, per-GB rate and the history. */}
          <Modal
            open={!!moneyFor}
            onClose={() => setMoneyFor(null)}
            title={t("accounting.moneySettingsFor", { name: moneyFor?.username || "" })}
          >
            {moneyFor && (
              <div className="space-y-4">
                {moneyFor.billing_mode === "usage" ? (
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("accounting.volumeBalance")}</label>
                    <div className="bg-gray-50 rounded-xl px-3 py-2.5 font-medium text-gray-700" dir="ltr">
                      {formatGb(moneyFor.volume_balance_gb)} GB
                    </div>
                    <div className="flex gap-2 mt-2">
                      <input
                        type="number"
                        className="input flex-1"
                        placeholder={t("accounting.volumeAmountPlaceholder")}
                        value={volTopup.amount}
                        onChange={(e) => setVolTopup((v) => ({ ...v, amount: e.target.value }))}
                      />
                      <input
                        className="input flex-1"
                        placeholder={t("accounting.expenseNote")}
                        value={volTopup.note}
                        onChange={(e) => setVolTopup((v) => ({ ...v, note: e.target.value }))}
                      />
                      <button type="button" className="btn-secondary shrink-0" disabled={volSaving || !Number(volTopup.amount)} onClick={doVolumeTopup}>
                        {volSaving ? "..." : t("accounting.apply")}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div>
                    <label className="block text-sm text-gray-600 mb-1">{t("accounting.creditBalance")}</label>
                    <div className="bg-gray-50 rounded-xl px-3 py-2.5 font-medium text-gray-700" dir="ltr">
                      {fmt(moneyFor.balance)}
                    </div>
                  </div>
                )}

                {isSuperadmin && (
                  <>
                    <div>
                      <label className="block text-sm text-gray-600 mb-1">{t("accounting.creditLimit")}</label>
                      <MoneyInput
                        value={moneyForm.credit_limit ?? 0}
                        onChange={(v) => setMoneyForm((f) => ({ ...f, credit_limit: v === "" ? 0 : Number(v) }))}
                      />
                      <div className="hint">{t("accounting.creditLimitHint")}</div>
                    </div>
                    <div>
                      <label className="block text-sm text-gray-600 mb-1">{t("accounting.perGbRate")}</label>
                      <MoneyInput
                        value={moneyForm.wholesale_price_per_gb ?? 0}
                        onChange={(v) => setMoneyForm((f) => ({ ...f, wholesale_price_per_gb: v === "" ? 0 : Number(v) }))}
                      />
                      <div className="hint">
                        {moneyFor.billing_mode === "usage" && !Number(moneyForm.wholesale_price_per_gb)
                          ? t("accounting.perGbRateMissing")
                          : t("accounting.perGbRateHint")}
                      </div>
                    </div>
                    {moneyError && <div className="text-sm text-red-500 bg-red-50 rounded-lg px-3 py-2">{moneyError}</div>}
                    {moneyMsg && <div className="text-sm text-emerald-600 bg-emerald-50 rounded-lg px-3 py-2">{moneyMsg}</div>}
                    <button type="button" className="btn-primary" disabled={moneySaving} onClick={saveMoneySettings}>
                      {moneySaving ? "..." : t("common.save")}
                    </button>
                  </>
                )}

                <div>
                  <div className="section-title mb-2">{t("accounting.changeHistory")}</div>
                  {moneyLogs === null ? (
                    <div className="text-xs text-gray-400 py-3">{t("common.loading")}</div>
                  ) : moneyLogs.length === 0 ? (
                    <div className="empty-state">{t("accounting.noChangesYet")}</div>
                  ) : (
                    <div className="max-h-56 overflow-y-auto divide-y divide-gray-50 border border-gray-100 rounded-xl">
                      {moneyLogs.map((l) => {
                        const gb = l.amount_gb !== undefined;
                        const amount = gb ? l.amount_gb : l.amount;
                        return (
                          <div key={l.id} className="flex items-center justify-between px-3 py-2 text-xs">
                            <div className="flex items-center gap-1.5">
                              {amount > 0 ? <TrendingUp size={13} className="text-emerald-500" /> : <TrendingDown size={13} className="text-red-500" />}
                              <span className={amount > 0 ? "text-emerald-600 font-medium" : "text-red-500 font-medium"} dir="ltr">
                                {amount > 0 ? "+" : ""}{gb ? `${formatGb(amount)} GB` : fmt(amount)}
                              </span>
                              {l.note && <span className="text-gray-400">· {l.note}</span>}
                            </div>
                            <div className="text-gray-400" dir="ltr">{l.created_by_username || "—"}</div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            )}
          </Modal>
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
