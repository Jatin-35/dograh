"use client";

import { Mail, MessageCircle, Phone, X } from "lucide-react";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { BrandLogo } from "@/components/BrandLogo";
import {
    hidesSupportLauncher,
    isChatwootConfigured,
    SUPPORT_EMAIL,
    SUPPORT_PHONE,
    SUPPORT_WHATSAPP,
    whatsappHref,
} from "@/lib/support";
import { cn } from "@/lib/utils";

/**
 * The bottom-right support affordance: a launcher button that opens a small
 * branded card.
 *
 * This replaces the third-party live-chat bubble the upstream image shipped,
 * which was wired to dograh-hq's Chatwoot server — wrong branding, and client
 * conversations landing in someone else's inbox. See `@/lib/support` for the
 * full reasoning and for how to switch real live chat back on.
 *
 * Deliberately self-contained: no network calls, no third-party script, no
 * state that outlives the page. It is also the seam for the richer version —
 * swap the card's body for a real assistant and the launcher, positioning and
 * route rules carry over unchanged.
 */
export default function SupportGreeter() {
    const pathname = usePathname();
    const [open, setOpen] = useState(false);
    const containerRef = useRef<HTMLDivElement>(null);

    // Collapse when navigating away, so the card never survives a route change
    // onto a surface that hides the launcher (which would strand it open with
    // no way to close it).
    useEffect(() => {
        setOpen(false);
    }, [pathname]);

    useEffect(() => {
        if (!open) return;

        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === "Escape") setOpen(false);
        };
        const onPointerDown = (event: PointerEvent) => {
            if (!containerRef.current?.contains(event.target as Node)) {
                setOpen(false);
            }
        };

        document.addEventListener("keydown", onKeyDown);
        document.addEventListener("pointerdown", onPointerDown);
        return () => {
            document.removeEventListener("keydown", onKeyDown);
            document.removeEventListener("pointerdown", onPointerDown);
        };
    }, [open]);

    // A deployment with its own live chat shows that instead — never both.
    if (isChatwootConfigured) return null;
    if (hidesSupportLauncher(pathname)) return null;

    const hasContactDetails = Boolean(
        SUPPORT_EMAIL || SUPPORT_PHONE || SUPPORT_WHATSAPP,
    );

    return (
        <div
            ref={containerRef}
            className="fixed bottom-6 right-6 z-50 flex flex-col items-end gap-3"
        >
            {open && (
                <div
                    role="dialog"
                    aria-label="Support"
                    className={cn(
                        "w-[min(20rem,calc(100vw-3rem))] rounded-xl border bg-popover p-4",
                        "text-popover-foreground shadow-lg",
                    )}
                >
                    <BrandLogo className="h-6" textClassName="text-base" />

                    <p className="mt-3 text-sm font-medium">Hi there 👋</p>
                    <p className="mt-1 text-sm text-muted-foreground">
                        {hasContactDetails
                            ? "Need a hand with your agents? Reach us here and we'll get back to you."
                            : "Need a hand with your agents? Get in touch with your BotrixAI contact and we'll help you out."}
                    </p>

                    {hasContactDetails && (
                        <div className="mt-4 flex flex-col gap-2">
                            {SUPPORT_EMAIL && (
                                <a
                                    href={`mailto:${SUPPORT_EMAIL}`}
                                    className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors hover:bg-accent hover:text-accent-foreground"
                                >
                                    <Mail className="size-4 shrink-0 text-muted-foreground" />
                                    <span className="truncate">{SUPPORT_EMAIL}</span>
                                </a>
                            )}
                            {SUPPORT_WHATSAPP && (
                                <a
                                    href={whatsappHref(SUPPORT_WHATSAPP)}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors hover:bg-accent hover:text-accent-foreground"
                                >
                                    <MessageCircle className="size-4 shrink-0 text-muted-foreground" />
                                    <span className="truncate">WhatsApp {SUPPORT_WHATSAPP}</span>
                                </a>
                            )}
                            {SUPPORT_PHONE && (
                                <a
                                    href={`tel:${SUPPORT_PHONE.replace(/\s+/g, "")}`}
                                    className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors hover:bg-accent hover:text-accent-foreground"
                                >
                                    <Phone className="size-4 shrink-0 text-muted-foreground" />
                                    <span className="truncate">{SUPPORT_PHONE}</span>
                                </a>
                            )}
                        </div>
                    )}
                </div>
            )}

            <button
                type="button"
                onClick={() => setOpen((wasOpen) => !wasOpen)}
                aria-expanded={open}
                aria-label={open ? "Close support" : "Need help?"}
                title={open ? "Close support" : "Need help?"}
                className={cn(
                    "flex size-12 items-center justify-center rounded-full",
                    "bg-primary text-primary-foreground shadow-lg transition-transform",
                    "hover:scale-105 focus-visible:outline-none focus-visible:ring-2",
                    "focus-visible:ring-ring focus-visible:ring-offset-2",
                )}
            >
                {open ? <X className="size-5" /> : <MessageCircle className="size-5" />}
            </button>
        </div>
    );
}
