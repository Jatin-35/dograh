// Shared dark two-column auth shell, used by BOTH the Stack Auth handler
// (/handler/[...stack], cloud) and the local/OSS auth pages (/auth/login,
// /auth/signup). LEFT (lg+ only): a brand/value panel with the Dograh logo,
// proof points, and a Bland-style enterprise CTA block at the bottom (passed
// in as `enterpriseSlot`). RIGHT: a centered card that wraps the auth form
// (`children`). Mobile collapses to the single card column. The form column
// scrolls and stays centered so tall (sign-up) forms never clip on short
// viewports. Palette is the app's blacks/greys with one warm CTA accent.

import { BarChart3, Megaphone, Puzzle, Workflow } from "lucide-react";
import type { ReactNode } from "react";

import { BrandLogo } from "@/components/BrandLogo";

const HIGHLIGHTS = [
  "Human-like voice",
  "Multi-language",
  "Real-time responses",
];

const FEATURES = [
  {
    icon: Workflow,
    title: "Visual workflow builder",
    description: "Design agent conversations as a drag-and-drop flow, no scripting required.",
  },
  {
    icon: Megaphone,
    title: "Outbound campaigns",
    description: "Launch and scale automated calling campaigns in minutes.",
  },
  {
    icon: Puzzle,
    title: "Tool calling",
    description: "Give agents real actions to take, not just talk.",
  },
  {
    icon: BarChart3,
    title: "Built-in analytics",
    description: "Call recordings, run history, and daily reports out of the box.",
  },
];

export function AuthShell({
  children,
  enterpriseSlot,
}: {
  children: ReactNode;
  enterpriseSlot?: ReactNode;
}) {
  return (
    <div className="grid min-h-screen w-full bg-background lg:grid-cols-[45%_55%]">
      {/* Brand / value panel (LEFT) — hidden on mobile */}
      <aside className="relative hidden flex-col justify-between overflow-hidden border-r border-border/60 bg-zinc-950 p-10 lg:flex xl:p-14">
        {/* Ambient depth: soft radial glow behind the content */}
        <div
          aria-hidden
          className="pointer-events-none absolute -left-24 top-1/3 size-[28rem] rounded-full opacity-20 blur-3xl"
          style={{ background: "radial-gradient(circle, var(--cta), transparent 70%)" }}
        />

        <div className="relative">
          <BrandLogo inverse className="h-12" textClassName="text-3xl" />
        </div>

        <div className="relative max-w-xl space-y-6">
          <div className="space-y-5">
            <h1 className="text-balance font-semibold leading-tight tracking-tight text-zinc-50">
              <span className="text-3xl xl:text-4xl">The voice AI platform</span>{" "}
              <span className="text-xl text-zinc-300 xl:text-2xl">for real conversations.</span>
            </h1>
            <p className="text-sm text-zinc-400">
              Build, launch, and scale voice agents that sound human.
            </p>
            <ul className="flex flex-wrap gap-2">
              {HIGHLIGHTS.map((point) => (
                <li
                  key={point}
                  className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-1 text-xs font-medium text-zinc-300"
                >
                  {point}
                </li>
              ))}
            </ul>
          </div>

          {/* Feature grid — slight perspective tilt + layered inset/drop
              shadows for a subtle raised, "3D chip" feel, kept understated
              to match the panel's minimal dark aesthetic. */}
          <div
            className="grid grid-cols-2 gap-3"
            style={{ transform: "perspective(1200px) rotateX(2deg)" }}
          >
            {FEATURES.map(({ icon: Icon, title, description }) => (
              <div
                key={title}
                className="group rounded-xl border border-white/10 bg-gradient-to-b from-white/[0.06] to-white/[0.015] p-3.5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.06),0_10px_20px_-14px_rgba(0,0,0,0.7)] transition-all duration-300 ease-out hover:-translate-y-1 hover:border-white/25 hover:shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1),0_16px_28px_-14px_rgba(0,0,0,0.8),0_0_24px_-6px_var(--cta)]"
              >
                <div className="mb-2 flex size-8 items-center justify-center rounded-lg border border-white/10 bg-gradient-to-b from-white/[0.14] to-white/[0.02] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.18)] transition-transform duration-300 ease-out group-hover:scale-110 group-hover:-rotate-3">
                  <Icon className="size-4 text-zinc-200" />
                </div>
                <h3 className="text-xs font-semibold text-zinc-100">{title}</h3>
                <p className="mt-1 text-[11px] leading-snug text-zinc-400">{description}</p>
              </div>
            ))}
          </div>
        </div>

        {/* Enterprise CTA block (Bland-style) — bottom margin lifts it off the
            viewport edge while justify-between keeps the column layout */}
        <div className="relative mb-12 max-w-xl space-y-3 rounded-xl border border-white/10 bg-white/[0.03] p-5 xl:mb-16">
          <h2 className="text-sm font-semibold text-zinc-100">
            Need on-prem, data residency &amp; a data perimeter?
          </h2>
          <p className="text-sm text-zinc-400">
            We deploy BotrixAI inside your environment for regulated and
            high-scale teams.
          </p>
          {enterpriseSlot}
        </div>
      </aside>

      {/* Form column (RIGHT) — scrolls and stays centered so tall forms never
          clip. Carries the giant faded "dograh" imprint along its bottom. */}
      <main className="auth-imprint flex min-h-screen flex-col overflow-y-auto">
        <div className="flex min-h-full items-center justify-center p-6 pb-16 sm:p-10 sm:pb-24">
          <div className="w-full max-w-md space-y-6 rounded-2xl border border-border/60 bg-card p-6 shadow-lg sm:p-8">
            {/* Mobile-only wordmark (brand panel is hidden) */}
            <div className="lg:hidden">
              <BrandLogo className="h-7" />
            </div>
            {children}
          </div>
        </div>
      </main>
    </div>
  );
}
