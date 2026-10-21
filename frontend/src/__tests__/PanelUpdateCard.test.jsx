import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import PanelUpdateCard from "../components/PanelUpdateCard.jsx";
import { translate } from "../i18n/translations.js";
import * as api from "../api/client.js";

vi.mock("../api/client.js", () => ({
  checkPanelUpdate: vi.fn(), applyPanelUpdate: vi.fn(), fetchPanelUpdateStatus: vi.fn(), resolvePanelUpdate: vi.fn(),
}));
vi.mock("../context/AuthContext.jsx", () => ({ useAuth: () => ({ build: { version: "1.0", commit: "old" } }) }));

const TARGET = "a".repeat(40);
const JOB = { job_id: "j".repeat(32), target: TARGET, updated: false, resolvable: false };
const lost = () => Object.assign(new Error("Network Error"), { response: undefined });
const show = () => render(<PanelUpdateCard t={(key, vars) => translate("en", key, vars)} />);
const tick = async (ms = 4000) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const flush = () => tick(0);

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  api.fetchPanelUpdateStatus.mockResolvedValue({ data: { status: "idle", updated: false } });
  api.checkPanelUpdate.mockResolvedValue({ data: { dirty: false, up_to_date: false, behind: 2, pending: ["x y"], target: TARGET } });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function openConfirm() {
  show();
  await flush();
  fireEvent.click(screen.getByRole("button", { name: "Check for updates" }));
  await flush();
  fireEvent.click(screen.getByRole("button", { name: "Download and install" }));
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "pw" } });
}

describe("panel update card", () => {
  it("resumes a running update from the server's durable state after a page reload", async () => {
    api.fetchPanelUpdateStatus.mockResolvedValue({ data: { ...JOB, status: "running", phase: "build" } });
    show();
    await flush();
    expect(screen.getByRole("status").textContent).toContain("build");
    expect(api.applyPanelUpdate).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Download and install" }).disabled).toBe(true);
  });

  it("starts exactly once for a double click, with the full target and the typed password", async () => {
    let answer;
    api.applyPanelUpdate.mockReturnValue(new Promise((resolve) => { answer = resolve; }));
    await openConfirm();
    const start = screen.getByRole("button", { name: "Start the update" });
    fireEvent.click(start);
    fireEvent.click(start);
    await act(async () => { answer({ data: { ...JOB, status: "queued", phase: "queued" } }); });
    expect(api.applyPanelUpdate).toHaveBeenCalledTimes(1);
    expect(api.applyPanelUpdate).toHaveBeenCalledWith(TARGET, "pw");
    expect(screen.getByRole("status").textContent).toContain("queued");
  });

  it("keeps following through a restart and lost network; success only from the final state", async () => {
    api.applyPanelUpdate.mockResolvedValue({ data: { ...JOB, status: "queued", phase: "queued" } });
    await openConfirm();
    fireEvent.click(screen.getByRole("button", { name: "Start the update" }));
    await flush();
    api.fetchPanelUpdateStatus.mockRejectedValueOnce(lost()).mockRejectedValueOnce(lost())
      .mockResolvedValueOnce({ data: { ...JOB, status: "running", phase: "frontend" } })
      .mockResolvedValue({ data: { ...JOB, status: "completed", phase: "completed", updated: true, commit: TARGET } });
    await tick();
    expect(screen.getByText(/restarting or unreachable/)).toBeTruthy();
    expect(screen.queryByText(/every check passed/)).toBeNull();
    await tick();
    await tick();
    expect(screen.getByRole("status").textContent).toContain("frontend");
    expect(screen.queryByText(/every check passed/)).toBeNull();
    await tick();
    expect(screen.getByText(/every check passed/)).toBeTruthy();
  });

  it("a backend answering is not success: completed for another commit is never shown as done", async () => {
    api.fetchPanelUpdateStatus.mockResolvedValue({ data: { ...JOB, status: "completed", updated: true, commit: "b".repeat(40) } });
    show();
    await flush();
    expect(screen.queryByText(/every check passed/)).toBeNull();
  });

  it("an unknown result blocks a new update and offers only the confirmed, password-checked close", async () => {
    api.fetchPanelUpdateStatus.mockResolvedValue({ data: { ...JOB, status: "unknown", error_code: "runner_lost", resolvable: true } });
    api.resolvePanelUpdate.mockResolvedValue({ data: { ...JOB, status: "abandoned" } });
    show();
    await flush();
    expect(screen.getByRole("alert").textContent).toContain("runner_lost");
    expect(screen.getByRole("button", { name: "Check for updates" }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "Download and install" }).disabled).toBe(true);
    const close = screen.getByRole("button", { name: "Close this update" });
    expect(close.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(screen.getByLabelText("Your password"), { target: { value: "pw" } });
    fireEvent.click(close);
    await flush();
    expect(api.resolvePanelUpdate).toHaveBeenCalledWith(JOB.job_id, "pw");
    expect(screen.getByText(/closed without a definite result/)).toBeTruthy();
  });

  it("a lost answer to the start request re-reads the job instead of assuming nothing happened", async () => {
    api.applyPanelUpdate.mockRejectedValue(lost());
    await openConfirm();
    api.fetchPanelUpdateStatus.mockResolvedValue({ data: { ...JOB, status: "running", phase: "backup" } });
    fireEvent.click(screen.getByRole("button", { name: "Start the update" }));
    await flush();
    expect(screen.getByRole("status").textContent).toContain("backup");
  });
});
