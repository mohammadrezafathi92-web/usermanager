// The password prompt must stay usable when it is raised from inside a Modal
// (which makes the app root inert while open).
import React, { useState } from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

let prompt = null;
vi.mock("../api/client.js", () => ({ setPasswordPrompt: (fn) => { prompt = fn; } }));

import ConfirmPasswordGate from "../components/ConfirmPasswordGate.jsx";
import Modal from "../components/Modal.jsx";
import { LanguageProvider } from "../context/LanguageContext.jsx";
import { translate } from "../i18n/translations.js";

const memory = new Map();
vi.stubGlobal("localStorage", {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
  removeItem: (key) => memory.delete(key),
  clear: () => memory.clear(),
});
afterEach(() => cleanup());

function App() {
  const [open] = useState(true);
  return <LanguageProvider>
    <ConfirmPasswordGate />
    <Modal open={open} onClose={() => {}} title="dialog"><button type="button">inside</button></Modal>
  </LanguageProvider>;
}

describe("password prompt over a dialog", () => {
  it("is outside the inert app root and accepts the password", async () => {
    document.body.innerHTML = '<div id="root"></div>';
    render(<App />, { container: document.getElementById("root") });
    expect(document.getElementById("root").hasAttribute("inert")).toBe(true);
    let answer;
    await act(async () => { answer = prompt({ retry: false }); });
    const input = await screen.findByPlaceholderText(translate("fa", "confirmDelete.placeholder"));
    expect(input.closest("[inert]")).toBeNull();
    expect(input.closest('[role="dialog"][aria-modal="true"]')).not.toBeNull();
    const user = userEvent.setup();
    await user.type(input, "secret");
    await user.click(screen.getByRole("button", { name: translate("fa", "confirmDelete.confirm") }));
    await expect(answer).resolves.toBe("secret");
  });
});
