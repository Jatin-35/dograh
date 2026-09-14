"use client";

import { useRef } from "react";

interface ResizeHandleProps {
    /** "vertical" is a vertical divider dragged left/right (resizing width);
     * "horizontal" is a horizontal divider dragged up/down (resizing height). */
    orientation: "vertical" | "horizontal";
    /** Pixels moved since the last event. The caller decides the sign, because
     * a panel on the right grows as the pointer moves left. */
    onDelta: (delta: number) => void;
    onDoubleClick?: () => void;
    label: string;
}

/**
 * A draggable divider, the way an editor does it.
 *
 * Pointer capture rather than window listeners: once the pointer is captured
 * the drag keeps tracking even when it moves over the Monaco canvas or off the
 * window, which is exactly where a naive mousemove handler loses it and the
 * panel sticks mid-drag.
 *
 * The hit area is deliberately wider than the visible line — a 1px target is
 * miserable to grab — using negative margins so it doesn't consume layout.
 */
export function ResizeHandle({ orientation, onDelta, onDoubleClick, label }: ResizeHandleProps) {
    const dragging = useRef(false);
    const last = useRef(0);

    const isVertical = orientation === "vertical";

    return (
        <div
            role="separator"
            aria-orientation={orientation}
            aria-label={label}
            tabIndex={0}
            className={
                isVertical
                    ? "group relative z-10 -mx-1 w-2 shrink-0 cursor-col-resize"
                    : "group relative z-10 -my-1 h-2 shrink-0 cursor-row-resize"
            }
            onPointerDown={(event) => {
                dragging.current = true;
                last.current = isVertical ? event.clientX : event.clientY;
                event.currentTarget.setPointerCapture(event.pointerId);
            }}
            onPointerMove={(event) => {
                if (!dragging.current) return;
                const current = isVertical ? event.clientX : event.clientY;
                onDelta(current - last.current);
                last.current = current;
            }}
            onPointerUp={(event) => {
                dragging.current = false;
                event.currentTarget.releasePointerCapture(event.pointerId);
            }}
            onDoubleClick={onDoubleClick}
            onKeyDown={(event) => {
                // Keyboard resize, since a drag handle is otherwise unreachable
                // without a pointer.
                const step = event.shiftKey ? 48 : 16;
                if (isVertical && event.key === "ArrowLeft") onDelta(-step);
                if (isVertical && event.key === "ArrowRight") onDelta(step);
                if (!isVertical && event.key === "ArrowUp") onDelta(-step);
                if (!isVertical && event.key === "ArrowDown") onDelta(step);
            }}
        >
            <div
                className={
                    isVertical
                        ? "absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-border transition-colors group-hover:bg-primary/50"
                        : "absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border transition-colors group-hover:bg-primary/50"
                }
            />
        </div>
    );
}
