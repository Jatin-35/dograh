"use client";

import { formatDistanceToNow } from "date-fns";
import { ArrowLeft, MessageSquarePlus } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import { useWorkflowGenChatSession } from "@/components/workflow-gen-chat/useWorkflowGenChatSession";
import { useWorkflowGenSessionList } from "@/components/workflow-gen-chat/useWorkflowGenSessionList";
import { WorkflowGenChatPanel } from "@/components/workflow-gen-chat/WorkflowGenChatPanel";
import { useAppConfig } from "@/context/AppConfigContext";
import { cn } from "@/lib/utils";

export default function WorkflowGenChatPage() {
    const { config, loading } = useAppConfig();
    const session = useWorkflowGenChatSession();
    const { sessions, refresh: refreshSessionList } = useWorkflowGenSessionList();

    // The sidebar's title/timestamp for the active thread only becomes
    // accurate once the in-flight turn finishes — the initial placeholder
    // title is set while sending, but the *real* agent-name title only
    // lands once a proposed workflow is confirmed (a `workflow_ready` event
    // from the confirm flow, not the send flow) — so refetch after either
    // transitions from busy to idle, not just after a send.
    const wasBusy = useRef(false);
    useEffect(() => {
        const isBusy = session.sendingMessage || session.confirming;
        if (wasBusy.current && !isBusy) {
            void refreshSessionList();
        }
        wasBusy.current = isBusy;
    }, [session.sendingMessage, session.confirming, refreshSessionList]);

    const handleNewThread = async () => {
        await session.startNewSession();
        await refreshSessionList();
    };

    if (loading) {
        return null;
    }

    if (!config?.workflowGenEnabled) {
        return (
            <div className="mx-auto flex h-screen max-w-2xl flex-col items-center justify-center gap-4 px-6 text-center">
                <h1 className="text-lg font-semibold">AI assistant not configured</h1>
                <p className="text-sm text-muted-foreground">
                    This deployment hasn&apos;t been set up with the environment variables the
                    in-product AI assistant needs (WF_GEN_LLM_PROVIDER and the matching
                    Azure OpenAI settings). Ask your operator to configure it.
                </p>
                <Button asChild variant="outline">
                    <Link href="/workflow">
                        <ArrowLeft className="h-4 w-4" />
                        Back to workflows
                    </Link>
                </Button>
            </div>
        );
    }

    return (
        <div className="flex h-screen flex-col">
            <header className="flex shrink-0 items-center gap-3 border-b border-border/70 px-4 py-3">
                <Button asChild variant="ghost" size="sm">
                    <Link href="/workflow">
                        <ArrowLeft className="h-4 w-4" />
                        Back
                    </Link>
                </Button>
                <div>
                    <h1 className="text-sm font-semibold">Build via Chat</h1>
                    <p className="text-xs text-muted-foreground">
                        Describe the voice agent you want — it can create, edit, and configure
                        agents using your own connected tools.
                    </p>
                </div>
            </header>

            <div className="flex min-h-0 flex-1">
                <aside className="flex w-64 shrink-0 flex-col border-r border-border/70">
                    <div className="shrink-0 p-3">
                        <Button onClick={() => void handleNewThread()} variant="outline" className="w-full justify-start gap-2">
                            <MessageSquarePlus className="h-4 w-4" />
                            New thread
                        </Button>
                    </div>
                    <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
                        {sessions.map((item) => {
                            const isActive = item.id === session.session?.id;
                            return (
                                <button
                                    key={item.id}
                                    type="button"
                                    onClick={() => void session.switchToSession(item.id)}
                                    className={cn(
                                        "w-full rounded-lg px-3 py-2 text-left transition-colors",
                                        isActive ? "bg-muted" : "hover:bg-muted/60",
                                    )}
                                >
                                    <p className={cn("truncate text-sm", isActive ? "text-foreground" : "text-foreground/80")}>{item.title}</p>
                                    <p className="mt-0.5 truncate text-xs text-muted-foreground">
                                        {formatDistanceToNow(new Date(item.updated_at), { addSuffix: true })}
                                    </p>
                                </button>
                            );
                        })}
                    </div>
                </aside>

                <div className="mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col">
                    <WorkflowGenChatPanel session={session} />
                </div>
            </div>
        </div>
    );
}
