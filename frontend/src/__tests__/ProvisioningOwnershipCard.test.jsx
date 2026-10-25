import React from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import ProvisioningOwnershipCard from "../components/ProvisioningOwnershipCard.jsx";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import * as api from "../api/client.js";

vi.mock("../api/client.js", () => ({
  fetchProvisioningStatus: vi.fn(),
  claimProvisioningOwnership: vi.fn(),
  reclaimProvisioningOwnership: vi.fn(),
}));

const show = () => render(<LanguageProvider><ProvisioningOwnershipCard t={(key) => key} /></LanguageProvider>);

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("localStorage", { getItem: () => "fa", setItem: vi.fn() });
  api.fetchProvisioningStatus.mockResolvedValue({ data: { runtime: { owner_state: "active", gate_mode: "enforced", ownership_action: null } } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows a healthy owner and offers no recovery action", async () => {
  show();
  await screen.findByText("provisioning.ownerActive");
  expect(screen.queryByRole("button", { name: "provisioning.ownerReclaim" })).toBeNull();
});

it("requires stopped-process confirmation, a reason, and the guarded reclaim call", async () => {
  api.fetchProvisioningStatus.mockResolvedValue({ data: { runtime: { owner_state: "active", gate_mode: "enforced", ownership_action: "reclaim" } } });
  api.reclaimProvisioningOwnership.mockResolvedValue({ data: { owner_state: "active", ownership_epoch: 2 } });
  show();
  fireEvent.click(await screen.findByRole("button", { name: "provisioning.ownerReclaim" }));
  const submit = screen.getAllByRole("button", { name: "provisioning.ownerReclaim" }).at(-1);
  expect(submit.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("provisioning.ownerReason"), { target: { value: "Host reboot confirmed; prior instance is stopped." } });
  fireEvent.click(screen.getByLabelText("provisioning.ownerPreviousStopped"));
  expect(submit.disabled).toBe(false);
  fireEvent.click(submit);
  await waitFor(() => expect(api.reclaimProvisioningOwnership).toHaveBeenCalledWith("Host reboot confirmed; prior instance is stopped."));
  await waitFor(() => expect(api.fetchProvisioningStatus).toHaveBeenCalledTimes(2));
});

it("never offers routine reclaim or forced takeover for a foreign owner", async () => {
  api.fetchProvisioningStatus.mockResolvedValue({ data: { runtime: { owner_state: "active", gate_mode: "enforced", ownership_action: "foreign_owner" } } });
  show();
  await screen.findByText("provisioning.ownerAction.foreign_owner");
  expect(screen.queryByRole("button", { name: "provisioning.ownerReclaim" })).toBeNull();
  expect(screen.queryByRole("button", { name: /takeover/i })).toBeNull();
});
