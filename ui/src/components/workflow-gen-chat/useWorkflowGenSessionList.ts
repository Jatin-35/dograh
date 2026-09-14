"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { listWorkflowGenSessionsApiV1WorkflowGenSessionsGet } from "@/client/sdk.gen";
import type { WorkflowGenChatSessionSummary } from "@/client/types.gen";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

/** Standalone-session history for the thread-history sidebar — standalone
 * entry point only (`ui/src/app/workflow/gen-chat/page.tsx`). The
 * per-workflow embedded panel has no sidebar and never uses this hook.
 *
 * `enabled` is false when Scout isn't available to this org, where the
 * endpoint answers 403 by design. Fetching anyway would greet the user with
 * an error toast about a feature they were never offered. */
export function useWorkflowGenSessionList({ enabled = true }: { enabled?: boolean } = {}) {
    const { isAuthenticated, loading: authLoading } = useAuth();
    const [sessions, setSessions] = useState<WorkflowGenChatSessionSummary[]>([]);
    const [loading, setLoading] = useState(true);
    const hasFetched = useRef(false);

    const refresh = useCallback(async () => {
        setLoading(true);
        try {
            const response = await listWorkflowGenSessionsApiV1WorkflowGenSessionsGet({});
            if (response.error) {
                throw new Error(detailFromError(response.error, "Failed to load past conversations"));
            }
            setSessions(response.data ?? []);
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to load past conversations");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        if (!enabled || authLoading || !isAuthenticated || hasFetched.current) return;
        hasFetched.current = true;
        void refresh();
    }, [enabled, authLoading, isAuthenticated, refresh]);

    return { sessions, loading, refresh };
}
