import { cn } from "@/lib/utils";

/** Four animated bars signalling that the assistant is working.
 *
 * Bars inherit `currentColor`, so this sits correctly on a button, in a
 * panel header, and in either theme without per-use styling. Styles live in
 * globals.css alongside the auth waveform it mirrors, which also carries the
 * prefers-reduced-motion guard. */
export function AssistantWave({ className }: { className?: string }) {
    return (
        <span className={cn("assistant-waveform", className)} role="status" aria-label="Working">
            <span />
            <span />
            <span />
            <span />
        </span>
    );
}
