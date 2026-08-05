/**
 * The five realistic apps of a cua-hub attempt, as a tab strip.
 *
 * A gym task is multi-app by construction — "order it in ShopGym and email me
 * the total" cannot be done in one storefront — so an annotator needs to move
 * between apps the way a person would. The backend already seeds all five and
 * returns them on open; this is what makes them reachable.
 *
 * Switching goes over the SAME socket as every other input, so the service
 * rebinds its screencast and mouse to the new tab (see `_bind` in
 * live_browser/service.py). Navigating the single tab instead would work too,
 * but it would lose the other apps' scroll position and in-page state, which an
 * annotator mid-task notices immediately.
 */
import { APP_COLOR } from "../../lib/appColors";
import type { AppKey } from "../../lib/types";
import type { LiveApp } from "./liveSessionApi";

export interface AppTabsProps {
  apps: LiveApp[];
  /** tabId reported by the last ack, or the app key we opened on. */
  activeApp?: string;
  /** Switch to an app. Returns false when the socket refused (not controller,
   *  or disconnected) so the caller can surface it rather than appear stuck. */
  onSwitch: (app: LiveApp) => boolean | void;
  disabled?: boolean;
}

export function AppTabs({ apps, activeApp, onSwitch, disabled }: AppTabsProps) {
  if (!apps.length) return null;
  return (
    <div
      role="tablist"
      aria-label="Gym apps"
      style={{
        display: "flex",
        gap: 6,
        alignItems: "center",
        padding: "6px 8px",
        borderBottom: "1px solid var(--border, #e5e7eb)",
        overflowX: "auto",
      }}
    >
      {apps.map((a) => {
        const active = a.app === activeApp;
        const hue = APP_COLOR[a.app as AppKey] ?? "#64748b";
        const broken = Boolean(a.error);
        return (
          <button
            key={a.app}
            role="tab"
            aria-selected={active}
            // A disabled tab still needs to say WHY, or a missing app reads as a
            // bug in the platform rather than an app that failed to prepare.
            title={broken ? `${a.title} unavailable — ${a.error}` : a.url}
            disabled={disabled || broken}
            onClick={() => !broken && onSwitch(a)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 10px",
              borderRadius: 999,
              border: `1px solid ${active ? hue : "var(--border, #e5e7eb)"}`,
              background: active ? `${hue}14` : "transparent",
              color: broken ? "var(--muted, #94a3b8)" : "inherit",
              fontWeight: active ? 600 : 400,
              fontSize: 13,
              cursor: broken || disabled ? "not-allowed" : "pointer",
              opacity: broken ? 0.55 : 1,
              whiteSpace: "nowrap",
            }}
          >
            <span
              aria-hidden
              style={{
                width: 8,
                height: 8,
                borderRadius: 999,
                background: broken ? "var(--muted, #cbd5e1)" : hue,
                flex: "0 0 auto",
              }}
            />
            {a.title}
          </button>
        );
      })}
    </div>
  );
}

export default AppTabs;
