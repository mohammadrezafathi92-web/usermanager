import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import NodeContractStatus from "../components/NodeContractStatus.jsx";

beforeEach(() => {
  const values = new Map();
  vi.stubGlobal("localStorage", { getItem: (k) => values.get(k) ?? null, setItem: (k, v) => values.set(k, String(v)) });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function show(status, props = {}, language = "fa") {
  localStorage.setItem("um_lang", language);
  return render(<LanguageProvider><NodeContractStatus status={status} {...props} /></LanguageProvider>);
}

describe("node contract status", () => {
  for (const language of ["fa", "en"]) {
    it(`distinguishes a verified contract from ordinary connectivity (${language})`, () => {
      show({ state: "ready", ready: true }, {}, language);
      expect(screen.getByRole("status").textContent).toContain(language === "fa" ? "قرارداد ساخت سرویس تأیید شده" : "Provisioning contract verified");
    });
    it(`shows failed save verification and a deliberate retry (${language})`, () => {
      const onVerify = vi.fn();
      show({ state: "failed", ready: false, reason: "probe_failed", retry_allowed: true }, { canVerify: true, onVerify }, language);
      expect(screen.getByRole("status").textContent).toContain(language === "fa" ? "تأیید نشد" : "not verified");
      fireEvent.click(screen.getByRole("button"));
      expect(onVerify).toHaveBeenCalledTimes(1);
    });
    it(`explains a failed automatic WireGuard interface setup (${language})`, () => {
      show({ state: "failed", ready: false, reason: "wireguard_interface_create_failed" }, {}, language);
      expect(screen.getByRole("status").textContent).toContain(language === "fa"
        ? "ساخت اینترفیس WireGuard روی روتر ناموفق بود"
        : "The WireGuard interface could not be created on the router");
    });
    it(`cleanup uncertainty blocks retry, including malicious retry flag (${language})`, () => {
      const onVerify = vi.fn();
      const onInspectCleanup = vi.fn();
      show({ state: "cleanup_unknown", ready: false, retry_allowed: true, cleanup_recovery_available: true,
        probe_identity: "um-probe-0123456789abcdef" }, { canVerify: true, onVerify, onInspectCleanup }, language);
      expect(screen.getByRole("button").textContent).toContain(language === "fa" ? "فقط‌خواندنی" : "read only");
      expect(screen.getByRole("status").textContent).toContain(language === "fa" ? "پاک‌سازی" : "cleanup");
      expect(onVerify).not.toHaveBeenCalled();
      expect(onInspectCleanup).not.toHaveBeenCalled();
    });
    it(`does not offer cleanup inspection without an eligible identity (${language})`, () => {
      show({ state: "cleanup_unknown", ready: false, cleanup_recovery_available: false },
        { canVerify: true, onVerify: vi.fn(), onInspectCleanup: vi.fn() }, language);
      expect(screen.queryByRole("button")).toBeNull();
    });
    it(`offers exact-identity cleanup inspection after a lost probe response (${language})`, () => {
      const onInspectCleanup = vi.fn();
      show({ state: "cleanup_unknown", ready: false, cleanup_recovery_available: false,
        cleanup_manual_identity_required: true }, { canVerify: true, onInspectCleanup }, language);
      expect(screen.getByRole("button").textContent).toContain(language === "fa" ? "فقط‌خواندنی" : "read only");
      expect(onInspectCleanup).not.toHaveBeenCalled();
    });
    it(`unknown state never appears ready or prints raw diagnostic content (${language})`, () => {
      show({ state: "unexpected", ready: true, reason: "password=secret https://user:pass@host/" }, {}, language);
      expect(screen.getByRole("status").textContent).not.toContain("secret");
      expect(screen.getByRole("status").textContent).not.toContain("Provisioning contract verified");
      expect(screen.getByRole("status").textContent).not.toContain("قرارداد ساخت سرویس تأیید شده");
    });
    it(`legacy payload is explicitly unknown, not ready (${language})`, () => {
      show(undefined, {}, language);
      expect(screen.getByRole("status").textContent).toContain(language === "fa" ? "نامشخص" : "unknown");
    });
  }
  it("does not automatically trigger a probe on render or reload", () => {
    const onVerify = vi.fn();
    show({ state: "stale", retry_allowed: true }, { canVerify: true, onVerify });
    expect(onVerify).not.toHaveBeenCalled();
  });
  it("granted-node viewers never get a verification action", () => {
    show({ state: "failed", retry_allowed: true }, { canVerify: false, onVerify: vi.fn() });
    expect(screen.queryByRole("button")).toBeNull();
  });
  it("busy feedback disables the deliberate action", () => {
    const onVerify = vi.fn();
    show({ state: "failed", retry_allowed: true }, { canVerify: true, busy: true, onVerify });
    const button = screen.getByRole("button");
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(onVerify).not.toHaveBeenCalled();
  });
});
