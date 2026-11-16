// Settings > Telemetry: Langfuse v4 project ID and trace visibility.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TelemetrySection } from "./TelemetrySection";

const api = vi.hoisted(() => ({
  get: vi.fn(),
  save: vi.fn(),
  remove: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }));

vi.mock("@/client/sdk.gen", () => ({
  getLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsGet: api.get,
  saveLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsPost: api.save,
  deleteLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsDelete: api.remove,
}));
vi.mock("sonner", () => ({ toast }));
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { id: "u1" }, loading: false }) }));

const saved = {
  host: "https://cloud.langfuse.com",
  public_key: "pk-lf-****1234",
  secret_key: "sk-lf-****abcd",
  project_id: "proj-77",
  traces_public: false,
  configured: true,
};

// Radix Switch measures itself; jsdom has no ResizeObserver.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

describe("TelemetrySection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the saved project ID and keeps public links off", async () => {
    api.get.mockResolvedValue({ data: saved });
    render(<TelemetrySection />);
    expect(await screen.findByDisplayValue("proj-77")).toBeTruthy();
    const toggle = screen.getByRole("switch", { name: /public trace links/i });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("sends the project ID and visibility when saving", async () => {
    api.get.mockResolvedValue({ data: saved });
    api.save.mockResolvedValue({ data: { project_id: "proj-77" } });
    render(<TelemetrySection />);
    await screen.findByDisplayValue("proj-77");

    fireEvent.click(screen.getByRole("switch", { name: /public trace links/i }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.save).toHaveBeenCalled());
    expect(api.save.mock.calls[0][0].body).toEqual({
      host: "https://cloud.langfuse.com",
      public_key: "pk-lf-****1234",
      secret_key: "sk-lf-****abcd",
      project_id: "proj-77",
      traces_public: true,
    });
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
  });

  it("warns when the project ID could not be detected", async () => {
    api.get.mockResolvedValue({ data: { ...saved, project_id: "" } });
    api.save.mockResolvedValue({ data: { project_id: "" } });
    render(<TelemetrySection />);
    await screen.findByDisplayValue("https://cloud.langfuse.com");

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.warning).toHaveBeenCalled());
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("reports a failed save", async () => {
    api.get.mockResolvedValue({ data: saved });
    api.save.mockResolvedValue({ error: { detail: "boom" } });
    render(<TelemetrySection />);
    await screen.findByDisplayValue("proj-77");

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });
});
