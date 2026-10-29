import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api/client.js", () => ({
  fetchProvisioningStatus: vi.fn(),
  fetchProvisioningOperation: vi.fn(),
  retryProvisioningOperation: vi.fn(),
  forceProvisioningStep: vi.fn(),
  manuallyAbandonUnknownXrayDelete: vi.fn(),
  fetchProvisioningOrphans: vi.fn(),
  fetchApprovalRecoveries: vi.fn(),
  decideApprovalRecovery: vi.fn(),
}));

import {
  fetchProvisioningStatus, fetchProvisioningOperation, manuallyAbandonUnknownXrayDelete,
  fetchProvisioningOrphans, fetchApprovalRecoveries,
} from "../api/client.js";
import ProvisioningCard from "../components/ProvisioningCard.jsx";

const status = { data: { p6_ready: false, blockers: [], operation_counts: {} } };
const operation = {
  data: {
    id: 182,
    operation_type: "delete_purchase",
    state: "provisioning",
    steps: [{ id: 470, node_id: 2, backend: "threexui", protocol: "xray",
      state: "remote_calling", error_code: "remote_result_unknown" }],
  },
};

describe("ProvisioningCard manual Xray recovery", () => {
  beforeEach(() => {
    fetchProvisioningStatus.mockResolvedValue(status);
    fetchProvisioningOperation.mockResolvedValue(operation);
    fetchProvisioningOrphans.mockResolvedValue({ data: [] });
    fetchApprovalRecoveries.mockResolvedValue({ data: [] });
    manuallyAbandonUnknownXrayDelete.mockResolvedValue({ data: { abandoned: true } });
    vi.spyOn(window, "prompt").mockReturnValue("Removed manually in 3X-UI");
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("offers a reasoned, confirmed manual recovery for only the unknown Xray delete step", async () => {
    render(<ProvisioningCard t={(key) => key} language="en" />);
    await waitFor(() => expect(fetchProvisioningStatus).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "182" } });
    fireEvent.click(screen.getByText("provisioning.show"));
    fireEvent.click(await screen.findByText("provisioning.manualXrayAbandon"));
    await waitFor(() => expect(manuallyAbandonUnknownXrayDelete).toHaveBeenCalledWith(
      182, 470, "Removed manually in 3X-UI"));
    expect(window.confirm).toHaveBeenCalledWith("provisioning.manualXrayAbandonConfirm");
  });

  it("hides manual recovery for a merely staged or non-Xray step", async () => {
    fetchProvisioningOperation.mockResolvedValue({ data: { ...operation.data,
      steps: [{ ...operation.data.steps[0], state: "staged" }] } });
    render(<ProvisioningCard t={(key) => key} language="en" />);
    await waitFor(() => expect(fetchProvisioningStatus).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "182" } });
    fireEvent.click(screen.getByText("provisioning.show"));
    await screen.findByText(/#182/);
    expect(screen.queryByText("provisioning.manualXrayAbandon")).toBeNull();
  });
});
