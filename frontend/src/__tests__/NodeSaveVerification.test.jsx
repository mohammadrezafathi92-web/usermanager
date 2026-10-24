import React from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import Nodes from "../pages/Nodes.jsx";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import * as api from "../api/client.js";

const state = vi.hoisted(() => ({ isSuperadmin: true, adminId: 1, confirm: vi.fn(), error: vi.fn() }));
vi.mock("../context/AuthContext.jsx", () => ({ useAuth: () => state }));
vi.mock("../components/Layout.jsx", () => ({ default: ({ children }) => <main>{children}</main> }));
vi.mock("../components/Topbar.jsx", () => ({ default: () => null }));
vi.mock("../components/Modal.jsx", () => ({ default: ({ open, children }) => open ? <div role="dialog">{children}</div> : null }));
vi.mock("../components/ConfirmDialog.jsx", () => ({ useConfirm: () => state.confirm }));
vi.mock("../components/Toast.jsx", () => ({ useToast: () => ({ error: state.error }) }));
vi.mock("../api/client.js", () => ({
  fetchNodes: vi.fn(), fetchNodeResources: vi.fn(), createNode: vi.fn(), updateNode: vi.fn(), deleteNode: vi.fn(), testNode: vi.fn(), verifyNodeContract: vi.fn(),
  pushRadiusConfig: vi.fn(), pushSstpConfig: vi.fn(), pushL2tpConfig: vi.fn(), pushIkev2Config: vi.fn(), importPppUsers: vi.fn(), importUserManagerUsers: vi.fn(), import3xuiClients: vi.fn(), rebuildNodeClients: vi.fn(),
}));
const node = { id: 7, name: "Test node", type: "softether", enabled: true, owner_admin_id: 1,
  se_host: "192.0.2.1", se_port: 443, se_hub_name: "DEFAULT", se_admin_password: "test-only",
  contract_status: { state: "failed", ready: false, reason: "verification_failed", retry_allowed: true } };
beforeEach(() => {
  vi.resetAllMocks(); state.isSuperadmin = true; state.confirm.mockResolvedValue(true);
  vi.stubGlobal("localStorage", { getItem: () => "en", setItem: vi.fn() });
  api.fetchNodes.mockResolvedValue({ data: [node] });
  api.fetchNodeResources.mockResolvedValue({ data: [] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const show = async () => { render(<LanguageProvider><Nodes /></LanguageProvider>); await screen.findByText("Test node"); };
const saveEditedNode = () => {
  fireEvent.click(screen.getAllByRole("button").find((button) => button.querySelector("svg.lucide-pencil")));
  const dialog = screen.getByRole("dialog");
  while (within(dialog).queryByRole("button", { name: "Next" })) fireEvent.click(within(dialog).getByRole("button", { name: "Next" }));
  fireEvent.click(within(dialog).getByRole("button", { name: /save server/i }));
};

it("list loading and ordinary connectivity testing never invoke contract verification", async () => {
  api.testNode.mockResolvedValue({ data: { ok: true } });
  await show();
  fireEvent.click(screen.getByRole("button", { name: /test connection/i }));
  await waitFor(() => expect(api.testNode).toHaveBeenCalledWith(7));
  expect(api.verifyNodeContract).not.toHaveBeenCalled();
  expect(screen.getByText("Provisioning contract not verified")).toBeTruthy();
});
it("explicit retry requires confirmation and cannot double-submit while waiting", async () => {
  let finish;
  api.verifyNodeContract.mockImplementation(() => new Promise((r) => { finish = r; }));
  await show();
  fireEvent.click(screen.getByRole("button", { name: "Verify contract again" }));
  await waitFor(() => expect(api.verifyNodeContract).toHaveBeenCalledTimes(1));
  expect(screen.getByRole("button", { name: /Verifying the server contract/ }).disabled).toBe(true);
  finish({ data: { ...node, contract_status: { state: "ready", ready: true, retry_allowed: true } } });
  await screen.findByText("Provisioning contract verified");
  expect(state.confirm).toHaveBeenCalledTimes(1);
});
it("cancelling retry makes no verification request", async () => {
  state.confirm.mockResolvedValue(false);
  await show();
  fireEvent.click(screen.getByRole("button", { name: "Verify contract again" }));
  await waitFor(() => expect(state.confirm).toHaveBeenCalledTimes(1));
  expect(api.verifyNodeContract).not.toHaveBeenCalled();
});
it("request failure refreshes status without automatically repeating the probe", async () => {
  api.verifyNodeContract.mockRejectedValue(new Error("timeout"));
  await show();
  fireEvent.click(screen.getByRole("button", { name: "Verify contract again" }));
  await waitFor(() => expect(state.error).toHaveBeenCalled());
  expect(api.verifyNodeContract).toHaveBeenCalledTimes(1);
  expect(api.fetchNodes).toHaveBeenCalledTimes(2);
});
it("uncertain request plus failed refresh cannot retain a previously ready card", async () => {
  api.fetchNodes.mockResolvedValueOnce({ data: [{ ...node, contract_status: { state: "ready", ready: true, retry_allowed: true } }] }).mockRejectedValue(new Error("offline"));
  api.verifyNodeContract.mockRejectedValue(new Error("response lost"));
  await show();
  expect(screen.getByText("Provisioning contract verified")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Verify contract again" }));
  await waitFor(() => expect(state.error).toHaveBeenCalled());
  expect(screen.queryByText("Provisioning contract verified")).toBeNull();
  expect(screen.queryByRole("button", { name: "Verify contract again" })).toBeNull();
  expect(screen.getByText("Provisioning contract status is unknown")).toBeTruthy();
});
it("a saved-but-failed verification is not reported as verified", async () => {
  api.fetchNodes.mockResolvedValueOnce({ data: [{ ...node, contract_status: { state: "ready", ready: true, retry_allowed: true } }] }).mockRejectedValue(new Error("offline"));
  api.updateNode.mockResolvedValue({ data: node });
  await show();
  // Existing pencil control has no label; selecting its actual edit action
  // avoids changing unrelated controls solely for the fixture.
  const edit = screen.getAllByRole("button").find((button) => button.querySelector("svg.lucide-pencil"));
  fireEvent.click(edit);
  const dialog = screen.getByRole("dialog");
  while (within(dialog).queryByRole("button", { name: "Next" })) fireEvent.click(within(dialog).getByRole("button", { name: "Next" }));
  fireEvent.click(within(dialog).getByRole("button", { name: /save server/i }));
  await waitFor(() => expect(api.updateNode).toHaveBeenCalledTimes(1));
  expect(api.updateNode.mock.calls[0][1]).not.toHaveProperty("contract_status");
  expect((await screen.findByRole("alert")).textContent).toContain("Settings were saved, but the server contract is not yet verified");
  expect(screen.queryByText("Provisioning contract verified")).toBeNull();
  expect(screen.getByText("Provisioning contract not verified")).toBeTruthy();
  expect(api.verifyNodeContract).not.toHaveBeenCalled();
});
it("lost save response does not claim failure or retain the old ready state", async () => {
  api.fetchNodes.mockResolvedValueOnce({ data: [{ ...node, contract_status: { state: "ready", ready: true, retry_allowed: true } }] }).mockRejectedValue(new Error("offline"));
  api.updateNode.mockRejectedValue(new Error("response lost"));
  await show(); saveEditedNode();
  await screen.findByText(/Settings may already have been saved/);
  expect(screen.queryByText("Provisioning contract verified")).toBeNull();
  expect(screen.queryByRole("button", { name: "Verify contract again" })).toBeNull();
  expect(screen.getByRole("button", { name: /save server/i }).disabled).toBe(true);
});
