import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import Subscription from "../pages/Subscription.jsx";
import QRCode from "qrcode";
import { fetchPublicSubscriptionInfo } from "../api/client.js";

vi.mock("../api/client.js", () => ({ fetchPublicSubscriptionInfo: vi.fn() }));
vi.mock("qrcode", () => ({ default: { toDataURL: vi.fn().mockResolvedValue("data:image/png;base64,") } }));
const gb = 1024 ** 3;
const storage = new Map();
vi.stubGlobal("localStorage", {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, String(value)),
});

function show(account, language = "en") {
  storage.set("um_lang", language);
  fetchPublicSubscriptionInfo.mockResolvedValue({ data: { accounts: [account] } });
  return render(<LanguageProvider><MemoryRouter initialEntries={["/s/test-token"]}>
    <Routes><Route path="/s/:token" element={<Subscription />} /></Routes>
  </MemoryRouter></LanguageProvider>);
}

function accountWithFourServices() {
  const purchases = [1, 2, 3, 4].map((id) => ({
    id, comment: `Amina${id}`, package_name: "Three users", status: "active",
    quota_bytes: (id === 1 ? 35 : 30) * gb, used_bytes: id * gb,
    max_concurrent_sessions: 3, expire_at: "2030-01-01T00:00:00", created_at: `2026-10-0${id}T00:00:00`,
  }));
  const connections = purchases.flatMap((p) => [1, 2].map((variant) => ({
    id: p.id * 10 + variant, purchase_id: p.id, purchase_batch: "same-old-batch",
    type: "openvpn", kind: "openvpn", comment: p.comment, created_at: p.created_at,
    total_bytes: 999 * gb, ovpn_files: [{ name: `GE-${variant}-p${p.id}-c${p.id * 10 + variant}.ovpn`, content: "fake-config" }],
  })));
  return { username: "customer", full_name: "Customer", status: "active", balance: 0,
    total_quota_bytes: 999 * gb, used_bytes: 888 * gb, purchases, connections };
}

beforeEach(() => {
  storage.clear();
  QRCode.toDataURL.mockResolvedValue("data:image/png;base64,");
});
afterEach(() => cleanup());

describe("subscription services", () => {
  it("shows each Purchase's usage even while its protocol configs are collapsed", async () => {
    show(accountWithFourServices());
    for (const id of [1, 2, 3, 4]) {
      const group = await screen.findByRole("region", { name: `Amina${id} #${id}` });
      expect(group.textContent).toContain(`${id}.0 GB / ${id === 1 ? 35 : 30} GB`);
      expect(group.textContent).toContain(`Remaining: ${id === 1 ? 34 : 30 - id} GB`);
      expect(group.textContent).toContain("Concurrent limit: 3");
      expect(within(group).getByRole("button").getAttribute("aria-expanded")).toBe("false");
    }
    expect(screen.queryByText(/888 GB/)).toBeNull();
    expect(screen.getAllByRole("progressbar")).toHaveLength(4);
  });

  it("keeps downloads on their own service and uses distinct file names", async () => {
    show(accountWithFourServices());
    const links = [];
    for (const id of [1, 2, 3, 4]) {
      const group = await screen.findByRole("region", { name: `Amina${id} #${id}` });
      fireEvent.click(within(group).getByRole("button"));
      const files = within(group).getAllByRole("link");
      expect(files).toHaveLength(2);
      for (const [i, link] of files.entries()) {
        expect(link.getAttribute("href")).toContain(`/config/${id * 10 + i + 1}?file=0`);
        links.push(link.getAttribute("download"));
      }
    }
    expect(new Set(links).size).toBe(8);
  });

  it("renders unlimited/zero and overquota correctly, including services with no connections", async () => {
    const account = accountWithFourServices();
    account.purchases = [
      { ...account.purchases[0], quota_bytes: 0, used_bytes: 0, expire_at: null },
      { ...account.purchases[1], quota_bytes: gb, used_bytes: 2 * gb, status: "quota_exceeded" },
    ];
    account.connections = [];
    show(account);
    const unlimited = await screen.findByRole("region", { name: "Amina1 #1" });
    expect(unlimited.textContent).toContain("0 B / Unlimited");
    expect(unlimited.textContent).toContain("No expiry");
    expect(within(unlimited).queryByRole("progressbar")).toBeNull();
    const exceeded = screen.getByRole("region", { name: "Amina2 #2" });
    expect(exceeded.textContent).toContain("2.0 GB / 1.0 GB");
    expect(exceeded.textContent).toContain("Remaining: 0 B");
    expect(within(exceeded).getByRole("progressbar").getAttribute("aria-valuenow")).toBe("100");
  });

  it("retains legacy shared usage when no Purchase metadata exists", async () => {
    show({ username: "legacy", status: "active", total_quota_bytes: 10 * gb, used_bytes: gb, connections: [] });
    expect(await screen.findByText("1.0 GB / 10 GB")).toBeTruthy();
  });

  it("shows Persian per-service labels and keeps technical values in bidi isolation", async () => {
    show(accountWithFourServices(), "fa");
    const group = await screen.findByRole("region", { name: "Amina1 #1" });
    expect(group.textContent).toContain("سقف هم‌زمان: 3 نفر");
    expect(group.textContent).toContain("باقی‌مانده");
    expect(group.querySelectorAll("bdi").length).toBeGreaterThan(1);
  });
});
