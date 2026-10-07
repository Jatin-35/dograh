/**
 * Inviting into an existing organization from Superadmin → Organizations.
 * Before this, the only invite was the one sent while creating the org, so
 * Think Gas's dead links (pointing at the retired server) couldn't be replaced.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
    list: vi.fn(),
    send: vi.fn(),
    revoke: vi.fn(),
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/superadminOrganizations", () => ({
    listOrganizationInvitations: (...a: unknown[]) => api.list(...a),
    sendOrganizationInvitation: (...a: unknown[]) => api.send(...a),
    revokeOrganizationInvitation: (...a: unknown[]) => api.revoke(...a),
}));
vi.mock("sonner", () => ({ toast }));

import { OrganizationInvitationsDialog } from "../OrganizationInvitationsDialog";

const THINK_GAS = { id: 5, name: "Think Gas" };
const LINK = "https://voicedashboard.botrixai.com/handler/team-invitation";
const pending = (id: string, email: string) => ({ id, email, expires_at: "2026-10-12T10:00:00+00:00" });

beforeEach(() => {
    Object.values(api).forEach((m) => m.mockReset());
    toast.success.mockReset();
    toast.error.mockReset();
    api.list.mockResolvedValue({ invitations: [pending("i1", "rahulkpandey2003@gmail.com")], link_opens_at: LINK });
    api.send.mockImplementation(async (_id: number, email: string) => ({ email, link_opens_at: LINK }));
    api.revoke.mockResolvedValue(undefined);
});

describe("OrganizationInvitationsDialog", () => {
    it("lists pending invitations and where links open", async () => {
        render(<OrganizationInvitationsDialog organization={THINK_GAS} onClose={vi.fn()} />);
        expect(await screen.findByText("rahulkpandey2003@gmail.com")).toBeTruthy();
        expect(api.list).toHaveBeenCalledWith(5);
        expect(screen.getByText("voicedashboard.botrixai.com")).toBeTruthy();
        expect(screen.getByText("Invite to Think Gas")).toBeTruthy();
    });

    it("sends an invite and refreshes the list", async () => {
        render(<OrganizationInvitationsDialog organization={THINK_GAS} onClose={vi.fn()} />);
        await screen.findByText("rahulkpandey2003@gmail.com");
        fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new.person@think-gas.com" } });
        await act(async () => {
            fireEvent.click(screen.getByRole("button", { name: "Send invite" }));
        });
        expect(api.send).toHaveBeenCalledWith(5, "new.person@think-gas.com");
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Invitation sent to new.person@think-gas.com."));
        expect(api.list).toHaveBeenCalledTimes(2);
        expect((screen.getByLabelText("Email") as HTMLInputElement).value).toBe("");
    });

    it("resends to a pending address (replacing the dead link)", async () => {
        render(<OrganizationInvitationsDialog organization={THINK_GAS} onClose={vi.fn()} />);
        await screen.findByText("rahulkpandey2003@gmail.com");
        await act(async () => {
            fireEvent.click(screen.getByRole("button", { name: "Resend to rahulkpandey2003@gmail.com" }));
        });
        expect(api.send).toHaveBeenCalledWith(5, "rahulkpandey2003@gmail.com");
    });

    it("revokes a pending invitation", async () => {
        render(<OrganizationInvitationsDialog organization={THINK_GAS} onClose={vi.fn()} />);
        await screen.findByText("rahulkpandey2003@gmail.com");
        await act(async () => {
            fireEvent.click(screen.getByRole("button", { name: "Revoke invitation to rahulkpandey2003@gmail.com" }));
        });
        expect(api.revoke).toHaveBeenCalledWith(5, "i1");
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
    });

    it("shows the auth provider's reason when sending fails", async () => {
        api.send.mockRejectedValue(new Error("Stack Auth team invitation failed (400): callback URL is not a trusted domain"));
        render(<OrganizationInvitationsDialog organization={THINK_GAS} onClose={vi.fn()} />);
        await screen.findByText("rahulkpandey2003@gmail.com");
        fireEvent.change(screen.getByLabelText("Email"), { target: { value: "x@think-gas.com" } });
        await act(async () => {
            fireEvent.click(screen.getByRole("button", { name: "Send invite" }));
        });
        expect(await screen.findByText(/not a trusted domain/)).toBeTruthy();
    });

    it("renders nothing when no organization is open", () => {
        render(<OrganizationInvitationsDialog organization={null} onClose={vi.fn()} />);
        expect(screen.queryByText(/Invite to/)).toBeNull();
        expect(api.list).not.toHaveBeenCalled();
    });
});
