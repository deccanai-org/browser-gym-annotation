/**
 * Chrome-style tab bar for the active browser window. Sits above the URL bar so
 * the annotator sees every app as a real browser tab, not a pill strip at the
 * bottom of the pane.
 */
import { t, weight } from "../../ds";
import { APP_COLOR } from "../../lib/appColors";
import type { AppKey } from "../../lib/types";
import type { LiveApp } from "./liveSessionApi";

export interface BrowserTabBarProps {
  apps: LiveApp[];
  activeApp?: string;
  disabled?: boolean;
  onSwitch: (app: LiveApp) => boolean | void;
}

export function BrowserTabBar({ apps, activeApp, disabled, onSwitch }: BrowserTabBarProps) {
  if (!apps.length) return null;
  return (
    <div
      role="tablist"
      aria-label="Browser tabs"
      style={{
        display: "flex",
        alignItems: "flex-end",
        gap: 2,
        padding: "0 8px",
        minHeight: 26,
        background: t.n8,
        borderBottom: `1px solid ${t.n7}`,
        fontFamily: t.fontPrimary,
        overflowX: "auto",
        flexShrink: 0,
      }}
    >
      {apps.map((a) => {
        const active = a.app === activeApp;
        const hue = APP_COLOR[a.app as AppKey] ?? t.n3;
        const broken = Boolean(a.error);
        return (
          <button
            key={a.app}
            role="tab"
            aria-selected={active}
            title={broken ? `${a.title} unavailable — ${a.error}` : a.url}
            disabled={disabled || broken}
            onClick={() => !broken && onSwitch(a)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 14px 5px",
              marginBottom: active ? -1 : 0,
              // Tab chrome keeps its browser shape; only the palette moved.
              borderRadius: `${t.radiusMd} ${t.radiusMd} 0 0`,
              border: `1px solid ${active ? t.n7 : "transparent"}`,
              borderBottom: `1px solid ${active ? t.n9 : "transparent"}`,
              background: active ? t.n9 : "transparent",
              color: broken ? t.n3 : active ? t.n0 : t.n2,
              fontWeight: active ? weight.semibold : weight.regular,
              fontSize: 12,
              cursor: broken || disabled ? "not-allowed" : "pointer",
              opacity: broken ? 0.55 : 1,
              whiteSpace: "nowrap",
              maxWidth: 160,
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            <span
              aria-hidden
              style={{
                width: 8,
                height: 8,
                borderRadius: t.radiusFull,
                background: broken ? t.n4 : hue,
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

export default BrowserTabBar;
