/**
 * Inactive gym apps as separate mini browser windows — the "real browser
 * ecosystem" the annotator works across. Only one app is live-screencast at a
 * time; the rest show a still of their own tab, photographed by the service
 * without fronting it, so a pre-opened app looks like the live page it is
 * instead of a placeholder nobody has visited yet.
 *
 * Clicking a window asks the parent to switch_tab over the socket, same as the
 * old pill strip — recording and replay stay url-addressed.
 */
import { t, weight } from "../../ds";
import { APP_COLOR } from "../../lib/appColors";
import type { AppKey } from "../../lib/types";
import type { LiveApp } from "./liveSessionApi";

export interface AppBrowserDockProps {
  apps: LiveApp[];
  activeApp?: string;
  /** base64 JPEG per app key — the service's capture of that tab, or the last
   *  screencast frame of the app being switched away from. */
  snapshots: Record<string, string>;
  disabled?: boolean;
  onFocus: (app: LiveApp) => boolean | void;
}

function originOf(url: string): string {
  try { return new URL(url).origin; } catch { return ""; }
}

/** Whether this app's tab is already open (matched by origin). */
export function appIsOpen(app: LiveApp, openOrigins: Set<string>): boolean {
  return openOrigins.has(originOf(app.url));
}

export function AppBrowserDock({ apps, activeApp, snapshots, disabled, onFocus }: AppBrowserDockProps) {
  const inactive = apps.filter((a) => a.app !== activeApp);
  if (inactive.length === 0) return null;

  return (
    <div
      aria-label="Other gym apps"
      style={{
        display: "flex",
        gap: 10,
        padding: "10px 12px",
        borderTop: `1px solid ${t.n7}`,
        background: t.n8,
        fontFamily: t.fontPrimary,
        overflowX: "auto",
        flexShrink: 0,
      }}
    >
      {inactive.map((a) => {
        const hue = APP_COLOR[a.app as AppKey] ?? t.n3;
        const broken = Boolean(a.error);
        const snap = snapshots[a.app];
        return (
          <button
            key={a.app}
            type="button"
            disabled={disabled || broken}
            title={broken ? `${a.title} unavailable — ${a.error}` : `Focus ${a.title}`}
            onClick={() => !broken && onFocus(a)}
            style={{
              flex: "0 0 auto",
              width: 168,
              padding: 0,
              border: "none",
              background: "transparent",
              cursor: broken || disabled ? "not-allowed" : "pointer",
              opacity: broken ? 0.5 : 1,
              textAlign: "left",
            }}
          >
            <div
              style={{
                borderRadius: t.radiusMd,
                overflow: "hidden",
                boxShadow: `${t.shadowMd}, 0 0 0 1px ${t.n7}`,
                background: t.n9,
              }}
            >
              {/* Window chrome */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 5,
                  padding: "5px 8px",
                  background: `linear-gradient(180deg, ${t.n9} 0%, ${t.n8} 100%)`,
                  borderBottom: `1px solid ${t.n7}`,
                }}
              >
                {/* OS traffic lights, not platform chrome — they carry the
                    "separate browser window" read, so they keep their
                    system colours rather than the brand ramp. */}
                <span style={{ display: "flex", gap: 3 }}>
                  {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
                    <span key={c} style={{ width: 7, height: 7, borderRadius: t.radiusFull, background: c }} />
                  ))}
                </span>
                <span
                  style={{
                    flex: 1,
                    fontSize: 10,
                    fontWeight: weight.semibold,
                    color: t.n1,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {a.title}
                </span>
                <span
                  aria-hidden
                  style={{ width: 6, height: 6, borderRadius: t.radiusFull, background: hue, flexShrink: 0 }}
                />
              </div>
              {/* Preview */}
              <div
                style={{
                  aspectRatio: "16 / 10",
                  background: snap
                    ? t.n9
                    : `linear-gradient(135deg, color-mix(in srgb, ${hue} 10%, transparent) 0%, ${t.n8} 100%)`,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  overflow: "hidden",
                }}
              >
                {snap ? (
                  <img
                    src={`data:image/jpeg;base64,${snap}`}
                    alt=""
                    style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                  />
                ) : (
                  <span style={{ fontSize: 11, color: t.n3, fontWeight: weight.medium }}>{a.title}</span>
                )}
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}

export default AppBrowserDock;
