import React from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import PanelUpdateCard from "../components/PanelUpdateCard.jsx";
import { translate } from "../i18n/translations.js";
import * as api from "../api/client.js";

// Review of PR #158 at ce3674f: the first three are the reviewer's
// reproductions (they fail there); the rest pin what the fix must not loosen.
vi.mock("../api/client.js", () => ({
  checkPanelUpdate: vi.fn(), applyPanelUpdate: vi.fn(), fetchPanelUpdateStatus: vi.fn(), resolvePanelUpdate: vi.fn(),
}));
vi.mock("../context/AuthContext.jsx", () => ({ useAuth: () => ({ build: { version: "1", commit: "old" } }) }));
const target = "a".repeat(40);
const flush = async (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const show = () => render(<PanelUpdateCard t={(k, v) => translate("en", k, v)} />);
const waitPolls = async (n) => { for (let i = 0; i < n; i += 1) await flush(4000); };
const install = () => screen.getByRole("button", { name: "Download and install" });
const checkNow = async () => { fireEvent.click(screen.getByRole("button", { name: "Check for updates" })); await flush(); };
beforeEach(() => { vi.useFakeTimers(); vi.resetAllMocks(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("allows explicit retry of a failed build after checkout already reached target", async () => {
  api.fetchPanelUpdateStatus.mockResolvedValue({ data: { status: "failed", phase: "build", target, error_code: "safe_update_failed", updated: false } });
  api.checkPanelUpdate.mockResolvedValue({ data: { target, dirty: false, up_to_date: true, behind: 0 } });
  show(); await flush(); await checkNow();
  expect(install().disabled).toBe(false);
});

it("keeps polling when Docker is transiently unreachable while a worker runs", async () => {
  api.fetchPanelUpdateStatus.mockResolvedValueOnce({ data: { status: "unknown", target, error_code: "docker_unreachable" } })
    .mockResolvedValue({ data: { status: "running", target, phase: "build" } });
  show(); await flush(); await flush(12000);
  expect(api.fetchPanelUpdateStatus.mock.calls.length).toBeGreaterThan(1);
});

it("retries initial status read when page opens during backend restart", async () => {
  api.fetchPanelUpdateStatus.mockRejectedValueOnce(new Error("network"))
    .mockResolvedValue({ data: { status: "running", target, phase: "build" } });
  show(); await flush(); await flush(12000);
  expect(api.fetchPanelUpdateStatus.mock.calls.length).toBeGreaterThan(1);
});

it("the retry is never automatic: it needs a new confirmation and password, for the same SHA", async () => {
  api.fetchPanelUpdateStatus.mockResolvedValue({ data: { status: "abandoned", phase: "build", target, updated: false } });
  api.checkPanelUpdate.mockResolvedValue({ data: { target, dirty: false, up_to_date: true, behind: 0 } });
  api.applyPanelUpdate.mockResolvedValue({ data: { status: "queued", phase: "queued", target, updated: false } });
  show(); await flush(); await checkNow(); await flush(12000);
  expect(api.applyPanelUpdate).not.toHaveBeenCalled();
  fireEvent.click(install());
  expect(screen.getByRole("button", { name: "Start the update" }).disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: "Start the update" }));
  await flush();
  expect(api.applyPanelUpdate).toHaveBeenCalledTimes(1);
  expect(api.applyPanelUpdate).toHaveBeenCalledWith(target, "pw");
});

it("an up-to-date checkout with a completed (or no) previous job is not offered again", async () => {
  api.checkPanelUpdate.mockResolvedValue({ data: { target, dirty: false, up_to_date: true, behind: 0 } });
  api.fetchPanelUpdateStatus.mockResolvedValue({ data: { status: "completed", target, updated: true, commit: target } });
  show(); await flush(); await checkNow();
  expect(install().disabled).toBe(true);
  cleanup();
  api.fetchPanelUpdateStatus.mockResolvedValue({ data: { status: "failed", target: "b".repeat(40), updated: false } });
  show(); await flush(); await checkNow();
  expect(install().disabled).toBe(true);
});

it("while the state is unknown or unreadable, reading goes on but nothing can be started", async () => {
  api.checkPanelUpdate.mockResolvedValue({ data: { target, dirty: false, up_to_date: false, behind: 1 } });
  api.fetchPanelUpdateStatus.mockResolvedValue({ data: { status: "unknown", target, error_code: "docker_unreachable" } });
  show(); await flush(); await waitPolls(3);
  expect(api.fetchPanelUpdateStatus.mock.calls.length).toBeGreaterThan(2);
  expect(install().disabled).toBe(true);
  cleanup();
  api.fetchPanelUpdateStatus.mockReset();
  api.fetchPanelUpdateStatus.mockRejectedValue(new Error("network"));
  show(); await flush(); await waitPolls(3);
  expect(api.fetchPanelUpdateStatus.mock.calls.length).toBeGreaterThan(2);
  expect(install().disabled).toBe(true);
  expect(api.applyPanelUpdate).not.toHaveBeenCalled();
});
