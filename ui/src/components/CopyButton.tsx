"use client";

import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";

/**
 * Small inline copy-to-clipboard button. Shows a brief check state and a toast
 * on success. Shared across the superadmin pages (organizations, agents).
 */
export function CopyButton({ value, label }: { value: string; label: string }) {
    const [copied, setCopied] = useState(false);

    const handleCopy = async (e: React.MouseEvent) => {
        // Stop propagation so a copy button placed inside a clickable parent
        // (e.g. a collapsible header) doesn't also trigger the parent's onClick.
        e.stopPropagation();
        try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            toast.success(`${label} copied`);
            setTimeout(() => setCopied(false), 1500);
        } catch {
            toast.error("Couldn't copy to clipboard");
        }
    };

    return (
        <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-6 w-6 shrink-0 text-muted-foreground hover:text-foreground"
            onClick={handleCopy}
            aria-label={`Copy ${label}`}
        >
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        </Button>
    );
}
