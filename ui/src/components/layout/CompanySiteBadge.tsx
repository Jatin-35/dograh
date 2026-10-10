"use client";

import { Globe } from "lucide-react";

import { COMPANY_BASE_URL } from "@/constants/brand";
import { cn } from "@/lib/utils";

/** Our own site, in the bordered pill GitHubStarBadge uses. Replaces that
 * badge in the agent editor header, which linked to upstream's repository. */
export function CompanySiteBadge({ className }: { className?: string }) {
  return (
    <a
      href={COMPANY_BASE_URL}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(
        "inline-flex items-center rounded-md border text-sm leading-none hover:opacity-80 transition-opacity",
        className
      )}
    >
      <span className="inline-flex items-center gap-1.5 rounded-md bg-muted px-2.5 py-1.5">
        <Globe className="h-4 w-4" />
        <span className="font-medium">BotrixAI</span>
      </span>
    </a>
  );
}
