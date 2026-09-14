"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";

import {
  CHATWOOT_BASE_URL,
  CHATWOOT_WEBSITE_TOKEN,
  hidesSupportLauncher,
  isChatwootConfigured,
} from "@/lib/support";

declare global {
  interface Window {
    chatwootSDK?: {
      run: (config: {
        websiteToken: string;
        baseUrl: string;
      }) => void;
    };
    chatwootSettings?: {
      position?: "left" | "right";
      type?: "standard" | "expanded_bubble";
      launcherTitle?: string;
    };
    $chatwoot?: {
      toggleBubbleVisibility?: (visibility: "hide" | "show") => void;
      toggle?: (state?: "open" | "close") => void;
    };
  }
}

export default function ChatwootWidget() {
  const pathname = usePathname();

  // Load the Chatwoot SDK exactly once for the lifetime of the app.
  useEffect(() => {
    // No inbox configured is the default, not a misconfiguration —
    // `SupportGreeter` takes the corner instead. Staying silent here keeps
    // every page load from logging a warning about the expected state.
    if (!isChatwootConfigured) {
      return;
    }

    // Prevent duplicate initialization
    if (window.chatwootSettings) {
      return;
    }

    // Configure Chatwoot widget settings
    window.chatwootSettings = {
      position: "right",
      type: "standard",
      launcherTitle: "Chat with us",
    };

    // Check if script is already loaded
    const existingScript = document.querySelector(
      `script[src="${CHATWOOT_BASE_URL}/packs/js/sdk.js"]`
    );

    if (existingScript) {
      // Script already exists, just initialize if SDK is available
      window.chatwootSDK?.run({
        websiteToken: CHATWOOT_WEBSITE_TOKEN,
        baseUrl: CHATWOOT_BASE_URL,
      });
      return;
    }

    // Create and inject the Chatwoot SDK script
    const script = document.createElement("script");
    script.src = `${CHATWOOT_BASE_URL}/packs/js/sdk.js`;
    script.async = true;
    script.defer = true;
    script.onload = () => {
      window.chatwootSDK?.run({
        websiteToken: CHATWOOT_WEBSITE_TOKEN,
        baseUrl: CHATWOOT_BASE_URL,
      });
    };

    document.body.appendChild(script);
  }, []);

  // Show/hide the bubble per route using Chatwoot's native API. We never tear
  // down and recreate the SDK — doing so left the bubble permanently hidden
  // once a user had visited the builder.
  useEffect(() => {
    const applyVisibility = () => {
      if (!window.$chatwoot) return;
      if (hidesSupportLauncher(pathname)) {
        window.$chatwoot.toggle?.("close");
        window.$chatwoot.toggleBubbleVisibility?.("hide");
      } else {
        window.$chatwoot.toggleBubbleVisibility?.("show");
      }
    };

    // Apply immediately only once the bubble holder is actually in the DOM.
    // `window.$chatwoot` exists synchronously after run(), but `.woot--bubble-holder`
    // is appended later when the widget iframe loads, and toggleBubbleVisibility()
    // dereferences it with no null check. When it's absent, fall through to
    // `chatwoot:ready`, which the SDK fires once the holder exists.
    if (window.$chatwoot && document.querySelector(".woot--bubble-holder")) {
      applyVisibility();
      return;
    }

    window.addEventListener("chatwoot:ready", applyVisibility, { once: true });
    return () => window.removeEventListener("chatwoot:ready", applyVisibility);
  }, [pathname]);

  return null;
}
