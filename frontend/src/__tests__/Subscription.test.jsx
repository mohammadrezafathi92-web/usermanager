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

function mixedAccount() {
  const purchases = [
    { id: 7, comment: "Office PPTP", package_name: "Old router", status: "active", quota_bytes: 10 * gb, used_bytes: gb,
      expire_at: "2030-01-01T00:00:00", created_at: "2026-10-01T00:00:00" },
    { id: 8, comment: "Phone", package_name: "V2Ray", status: "active", quota_bytes: 20 * gb, used_bytes: gb,
      expire_at: "2030-01-01T00:00:00", created_at: "2026-10-02T00:00:00" },
  ];
  const connections = [
    { id: 71, purchase_id: 7, type: "pptp", kind: "pptp", node_name: "mt-1", link: null,
      config_text: "Server: vpn.example\nType: PPTP\nUsername: office\nPassword: x", created_at: "2026-10-01T00:00:00" },
    { id: 81, purchase_id: 8, type: "xray", kind: "vless", node_name: "x-1",
      link: "vless://uuid@x.example:443?security=reality#phone", created_at: "2026-10-02T00:00:00" },
  ];
  return { username: "mixed", status: "active", balance: 0, total_quota_bytes: 0, used_bytes: 0, purchases, connections };
}

describe("subscription protocols", () => {
  it("shows a PPTP service as PPTP with its own config, never as V2Ray", async () => {
    show(mixedAccount());
    const pptp = await screen.findByRole("region", { name: "Office PPTP #7" });
    fireEvent.click(within(pptp).getByRole("button"));
    expect(pptp.textContent).toContain("PPTP");
    expect(pptp.textContent).not.toContain("V2Ray");
    expect(within(pptp).getByDisplayValue(/Type: PPTP/)).toBeTruthy();
    expect(within(pptp).getByRole("link").getAttribute("download")).toBe("mt-1.txt");
  });

  it("shows a V2Ray service as V2Ray with its link, in its own service", async () => {
    show(mixedAccount());
    const xray = await screen.findByRole("region", { name: "Phone #8" });
    fireEvent.click(within(xray).getByRole("button"));
    expect(xray.textContent).toContain("V2Ray / Xray");
    expect(xray.textContent).not.toContain("PPTP");
    expect(within(xray).getByDisplayValue(/^vless:\/\//)).toBeTruthy();
  });

  it("offers the V2Ray app subscribe link only when there is a V2Ray service, and says what it carries", async () => {
    show(mixedAccount());
    const card = await screen.findByTestId("app-subscribe");
    expect(card.textContent).toContain("V2Ray/Xray");
    cleanup();
    const pptpOnly = mixedAccount();
    pptpOnly.purchases = pptpOnly.purchases.slice(0, 1);
    pptpOnly.connections = pptpOnly.connections.slice(0, 1);
    show(pptpOnly);
    await screen.findByRole("region", { name: "Office PPTP #7" });
    expect(screen.queryByTestId("app-subscribe")).toBeNull();
  });

  it("names an unknown protocol by itself instead of borrowing V2Ray's label", async () => {
    const account = mixedAccount();
    account.purchases = account.purchases.slice(0, 1);
    account.connections = [{ ...account.connections[0], type: "newproto", kind: "newproto" }];
    show(account);
    const group = await screen.findByRole("region", { name: "Office PPTP #7" });  // a single service opens by itself
    expect(group.textContent).toContain("NEWPROTO");
    expect(group.textContent).not.toContain("V2Ray");
  });
});

