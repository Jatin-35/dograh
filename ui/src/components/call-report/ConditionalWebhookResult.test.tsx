import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
    ConditionalWebhookResult,
    prettyBody,
    receiverMessage,
    webhookStatus,
} from "./ConditionalWebhookResult";

describe("ConditionalWebhookResult", () => {
    it("shows a delivered WhatsApp with what was sent and what came back", () => {
        render(
            <ConditionalWebhookResult
                result={{
                    name: "WhatsApp to Area Manager",
                    status: "delivered",
                    sent: true,
                    http_status: 200,
                    attempts: 1,
                    response: '{"status":"sent"}',
                    request: {
                        method: "POST",
                        url: "https://pannel.ailifebot.com/API_V2/Whatsapp/send_template/Y0xL••••",
                        payload: { templateId: "sales_person_voice", sender_phone: "917006485067" },
                    },
                }}
            />,
        );
        expect(screen.getByText("WhatsApp to Area Manager")).toBeTruthy();
        expect(screen.getByText("Delivered")).toBeTruthy();
        expect(screen.getByText(/send_template\/Y0xL••••/)).toBeTruthy();
        expect(screen.getByText(/"templateId": "sales_person_voice"/)).toBeTruthy();
        expect(screen.getByText(/HTTP 200 · attempt 1/)).toBeTruthy();
        expect(screen.getByText(/"status": "sent"/)).toBeTruthy();
    });

    it("shows why WhatsApp rejected it", () => {
        render(
            <ConditionalWebhookResult
                result={{
                    name: "WhatsApp to Area Manager",
                    status: "failed",
                    http_status: 400,
                    error: "HTTP 400: ...",
                    response: '{"detail":{"message":"(#132001) Template name does not exist in the translation"}}',
                }}
            />,
        );
        expect(screen.getByText("Failed")).toBeTruthy();
        expect(screen.getByText(/HTTP 400/)).toBeTruthy();
        // WhatsApp's own message as a callout, and the full reply in the panel.
        expect(
            screen.getByText("(#132001) Template name does not exist in the translation"),
        ).toBeTruthy();
        expect(screen.getAllByText(/Template name does not exist/)).toHaveLength(2);
    });

    it("says which rule stopped it", () => {
        render(
            <ConditionalWebhookResult
                result={{
                    name: "WhatsApp to customer",
                    status: "not_sent",
                    reason: "conditions_not_met",
                    failed_conditions: ["gathered_context.area_manager_number is not empty"],
                }}
            />,
        );
        expect(screen.getByText("Not sent")).toBeTruthy();
        expect(screen.getByText("gathered_context.area_manager_number is not empty")).toBeTruthy();
    });

    it("reads entries written before statuses existed", () => {
        expect(webhookStatus({ sent: true })).toBe("queued");
        expect(webhookStatus({ sent: false })).toBe("not_sent");
        expect(prettyBody("not json")).toBe("not json");
        expect(prettyBody('{"a":1}')).toBe('{\n  "a": 1\n}');
    });

    it("pulls the receiver's explanation out of common error shapes", () => {
        expect(receiverMessage('{"detail":{"message":"bad template"}}')).toBe("bad template");
        expect(receiverMessage('{"error":{"message":"invalid token"}}')).toBe("invalid token");
        expect(receiverMessage('{"error":"nope"}')).toBe("nope");
        expect(receiverMessage('{"message":"rate limited"}')).toBe("rate limited");
        expect(receiverMessage('{"status":"sent"}')).toBeNull();
        expect(receiverMessage("<html>502</html>")).toBeNull();
        expect(receiverMessage(undefined)).toBeNull();
    });
});
