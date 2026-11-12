/**
 * The Enable Embedding switch shows what is saved, every time the dialog opens.
 *
 * @vitest-environment jsdom
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowConfigurations } from "@/types/workflow-configurations";

import { EmbedDialog } from "./EmbedDialog";

const getToken = vi.fn();
const saveToken = vi.fn();
const deactivateToken = vi.fn();

vi.mock("@/client/sdk.gen", () => ({
    getEmbedTokenApiV1WorkflowWorkflowIdEmbedTokenGet: (...args: unknown[]) => getToken(...args),
    createOrUpdateEmbedTokenApiV1WorkflowWorkflowIdEmbedTokenPost: (...args: unknown[]) => saveToken(...args),
    deactivateEmbedTokenApiV1WorkflowWorkflowIdEmbedTokenDelete: (...args: unknown[]) => deactivateToken(...args),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

beforeAll(() => {
    // jsdom lacks these; Radix Dialog, Switch and Select use them.
    globalThis.ResizeObserver ??= class {
        observe() {}
        unobserve() {}
        disconnect() {}
    };
    Element.prototype.hasPointerCapture ??= () => false;
    Element.prototype.releasePointerCapture ??= () => {};
    Element.prototype.scrollIntoView ??= () => {};
});

const ACTIVE_TOKEN = {
    id: 1,
    token: "emb_TEST",
    allowed_domains: null,
    settings: { widgetType: "chat", embedMode: "floating" },
    is_active: true,
    usage_count: 0,
    usage_limit: null,
    embed_script: "<script></script>",
};

function renderDialog(open: boolean) {
    return (
        <EmbedDialog
            open={open}
            onOpenChange={() => {}}
            workflowId={7}
            workflowName="Test agent"
            workflowConfigurations={{} as WorkflowConfigurations}
            textChatInactivityTimeoutConstraints={null}
            widgetTextDefaults={null}
            onSaveWorkflowConfigurations={vi.fn().mockResolvedValue(undefined)}
        />
    );
}

async function openedSwitch() {
    const toggle = await screen.findByRole("switch", { name: /enable embedding/i });
    await waitFor(() => expect(getToken).toHaveBeenCalled());
    return toggle;
}

describe("Enable Embedding switch", () => {
    beforeEach(() => {
        getToken.mockReset();
        saveToken.mockReset();
        deactivateToken.mockReset();
    });

    it("comes back off when it was switched on but not saved", async () => {
        getToken.mockResolvedValue({ data: null });
        const { rerender } = render(renderDialog(true));

        const toggle = await openedSwitch();
        expect(toggle.getAttribute("aria-checked")).toBe("false");
        fireEvent.click(toggle);
        expect(toggle.getAttribute("aria-checked")).toBe("true");

        // Close without saving, then open again.
        rerender(renderDialog(false));
        getToken.mockClear();
        await act(async () => {
            rerender(renderDialog(true));
        });

        const reopened = await openedSwitch();
        await waitFor(() => expect(reopened.getAttribute("aria-checked")).toBe("false"));
        expect(saveToken).not.toHaveBeenCalled();
    });

    it("comes back on when embedding is saved as on", async () => {
        getToken.mockResolvedValue({ data: ACTIVE_TOKEN });
        render(renderDialog(true));

        const toggle = await openedSwitch();
        await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
    });

    it("comes back on when it was switched off but not saved", async () => {
        getToken.mockResolvedValue({ data: ACTIVE_TOKEN });
        const { rerender } = render(renderDialog(true));

        const toggle = await openedSwitch();
        await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
        fireEvent.click(toggle);
        expect(toggle.getAttribute("aria-checked")).toBe("false");

        rerender(renderDialog(false));
        getToken.mockClear();
        await act(async () => {
            rerender(renderDialog(true));
        });

        const reopened = await openedSwitch();
        await waitFor(() => expect(reopened.getAttribute("aria-checked")).toBe("true"));
        expect(deactivateToken).not.toHaveBeenCalled();
    });

    it("stays off after it is saved as off", async () => {
        getToken.mockResolvedValueOnce({ data: ACTIVE_TOKEN });
        deactivateToken.mockResolvedValue({ data: { message: "ok" } });
        const { rerender } = render(renderDialog(true));

        const toggle = await openedSwitch();
        await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
        fireEvent.click(toggle);
        // The settings are hidden once switched off; the save must still be there.
        fireEvent.click(screen.getByRole("button", { name: /save configurations/i }));
        await waitFor(() => expect(deactivateToken).toHaveBeenCalled());
        expect(saveToken).not.toHaveBeenCalled();
        // Saved as off: nothing left to save.
        await waitFor(() =>
            expect(screen.queryByRole("button", { name: /save configurations/i })).toBeNull(),
        );

        // The server now has no active token.
        getToken.mockResolvedValue({ data: null });
        rerender(renderDialog(false));
        getToken.mockClear();
        await act(async () => {
            rerender(renderDialog(true));
        });

        const reopened = await openedSwitch();
        await waitFor(() => expect(reopened.getAttribute("aria-checked")).toBe("false"));
    });
});
