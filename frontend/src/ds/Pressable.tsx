import type { CSSProperties, ReactNode } from "react";
import { t } from "./tokens";

/**
 * A control that is actually a control.
 *
 * Measured in a real browser: the review screen had 57 click handlers and ONE
 * focusable element. Verdict pills, fork commands, "make this the head" — all
 * spans and divs with onClick. That is invisible to a screen reader, unreachable
 * by keyboard, and shows no focus ring, so an annotator who works this screen all
 * day cannot keep their hands on the keys. The component tests never caught it
 * because testing-library's click() fires on any element at all.
 *
 * Renders a real <button>, styling stripped back to nothing so it can look like
 * whatever it needs to, with the affordances a button carries for free: Tab
 * order, Enter and Space, disabled semantics, and a visible focus ring.
 */
export function Pressable({
  children,
  onClick,
  disabled = false,
  title,
  label,
  style,
  pressed,
}: {
  children: ReactNode;
  onClick?: (e: React.MouseEvent) => void;
  disabled?: boolean;
  title?: string;
  /** Use when the visible text alone does not say what the control does. */
  label?: string;
  style?: CSSProperties;
  /** For a control that toggles, so its state is announced rather than only shown. */
  pressed?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      title={title}
      aria-label={label}
      aria-pressed={pressed}
      style={{
        font: "inherit",
        color: "inherit",
        background: "none",
        border: "none",
        padding: 0,
        margin: 0,
        textAlign: "inherit",
        cursor: disabled ? "not-allowed" : "pointer",
        outlineOffset: 2,
        outlineColor: t.primary6,
        ...style,
      }}
    >
      {children}
    </button>
  );
}
