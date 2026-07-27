import { cn } from "@/lib/utils";

// Reusable BotrixAI wordmark. Theme-aware by default: the dark logo shows on light
// surfaces and the light/cream logo shows on dark. Pass `inverse` to force the
// light logo on an always-dark surface (e.g. the auth brand panel). Pass `mark`
// to render the square logo mark instead of the full wordmark (e.g. the app
// sidebar header). Height is controlled by the caller via className (e.g.
// "h-7"); width stays auto so each lockup keeps its aspect ratio.
export function BrandLogo({
  className,
  textClassName = "text-xl",
  inverse = false,
  mark = false,
}: {
  className?: string;
  textClassName?: string;
  inverse?: boolean;
  mark?: boolean;
}) {
  if (mark) {
    return (
      <>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/BotrixAI-mark.png" alt="Botrix AI" className={cn("block w-auto select-none dark:hidden", className)} />
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/BotrixAI-mark-dark.png" alt="Botrix AI" className={cn("hidden w-auto select-none dark:block", className)} />
      </>
    );
  }
  if (inverse) {
    return (
      <span className={cn("flex items-center gap-2", className)}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/BotrixAI-mark-dark.png" alt="BotrixAI" className="h-full w-auto select-none" />
        <span className={cn("font-semibold tracking-tight text-zinc-50", textClassName)}>BotrixAI</span>
      </span>
    );
  }
  return (
    <span className={cn("flex items-center gap-2", className)}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/BotrixAI-mark.png" alt="BotrixAI" className="block h-full w-auto select-none dark:hidden" />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src="/BotrixAI-mark-dark.png" alt="BotrixAI" className="hidden h-full w-auto select-none dark:block" />
      <span className={cn("font-semibold tracking-tight text-foreground", textClassName)}>BotrixAI</span>
    </span>
  );
}
