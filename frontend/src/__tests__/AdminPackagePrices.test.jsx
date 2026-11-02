// Own cooperation price per package. Every API call is a mock; real fa/en
// translations are used.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import AdminPackagePrices from "../components/AdminPackagePrices.jsx";
import { translate, translations } from "../i18n/translations.js";
import * as api from "../api/client.js";

vi.mock("../api/client.js", () => ({ fetchAdminPackagePrices: vi.fn(), setAdminPackagePrice: vi.fn() }));

const ROWS = [
  { package_id: 1, package_name: "20 گیگ", quota_gb: 20, price: 20000, source: "per_gb_rate", own_price: null },
  { package_id: 2, package_name: "نامحدود", quota_gb: 0, price: 90000, source: "own_price", own_price: 90000 },
];
const t = (key, vars) => translate("en", key, vars);
const show = () => render(<AdminPackagePrices adminId={7} t={t} />);

beforeEach(() => {
  vi.resetAllMocks();
  api.fetchAdminPackagePrices.mockResolvedValue({ data: ROWS });
});
afterEach(() => cleanup());

describe("own package prices", () => {
  it("every key exists in both languages", () => {
    const keys = Object.keys(translations.en).filter((k) => k.startsWith("packagePrices."));
    expect(keys.length).toBeGreaterThan(10);
    for (const key of keys) expect(translations.fa[key]).toBeTruthy();
  });

  it("shows the price AND the rule that decided it, so own price and per-GB rate are never confused", async () => {
    show();
    expect(await screen.findByText(t("packagePrices.source.per_gb_rate"))).toBeTruthy();
    expect(screen.getByText(t("packagePrices.source.own_price"))).toBeTruthy();
    expect(screen.getByText(t("packagePrices.rateNote"))).toBeTruthy();
    expect(api.fetchAdminPackagePrices).toHaveBeenCalledWith(7);
  });

  it("saves a typed own price as a number", async () => {
    api.setAdminPackagePrice.mockResolvedValue({ data: { ...ROWS[0], price: 15000, source: "own_price", own_price: 15000 } });
    show();
    const input = await screen.findByLabelText(t("packagePrices.ownPriceFor", { name: "20 گیگ" }));
    fireEvent.change(input, { target: { value: "15,000" } });
    fireEvent.click(screen.getAllByRole("button", { name: t("common.save") })[0]);
    await waitFor(() => expect(api.setAdminPackagePrice).toHaveBeenCalledWith(7, 1, 15000));
    expect(await screen.findAllByText(t("packagePrices.source.own_price"))).toHaveLength(2);
  });

  it("clear sends null (falls back), never zero", async () => {
    api.setAdminPackagePrice.mockResolvedValue({ data: { ...ROWS[1], source: "package_cooperation", own_price: null } });
    show();
    fireEvent.click(await screen.findByRole("button", { name: t("packagePrices.clear") }));
    await waitFor(() => expect(api.setAdminPackagePrice).toHaveBeenCalledWith(7, 2, null));
  });

  it("a refused save shows the server's reason", async () => {
    api.setAdminPackagePrice.mockRejectedValue({ response: { data: { detail: "below cost" } } });
    show();
    const input = await screen.findByLabelText(t("packagePrices.ownPriceFor", { name: "20 گیگ" }));
    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.click(screen.getAllByRole("button", { name: t("common.save") })[0]);
    expect(await screen.findByText("below cost")).toBeTruthy();
  });
});
