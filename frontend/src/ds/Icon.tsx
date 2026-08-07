import type { CSSProperties } from "react";

/**
 * Inline SVG icon set used across the Task Review screen. Stroke-based on
 * currentColor by default; a few are filled (play). Paths trace the design.
 */
export type IconName =
  | "chevronLeft"
  | "chevronRight"
  | "close"
  | "plus"
  | "lock"
  | "reload"
  | "play"
  | "pause"
  | "skipStart"
  | "skipEnd"
  | "pencil"
  | "check"
  | "checkSquare"
  | "branch"
  | "alert"
  | "swap"
  | "expand"
  | "collapse";

const STROKE: Record<string, string> = {
  chevronLeft: "M15 6l-6 6 6 6",
  chevronRight: "M9 6l6 6-6 6",
  close: "M6 6l12 12M18 6l-12 12",
  plus: "M12 5v14M5 12h14",
  reload: "M20 11a8 8 0 10-2.3 5.7M20 20v-4h-4",
  pencil: "M16.5 3.5a2.1 2.1 0 013 3L8 18l-4 1 1-4 11.5-11.5z",
  check: "M5 12.5l4.2 4.2L19 7",
  skipStart: "M7 6v12",
  skipEnd: "M17 6v12",
  swap: "M7 8h13M7 8l3-3M7 8l3 3M17 16H4M17 16l-3-3M17 16l-3 3",
  // Give a panel's space to the work surface, and hand it back. Corner
  // arrows rather than a chevron, because this widens what you are working
  // ON — it is not navigation.
  expand: "M9 4H4v5M4 4l6 6M15 20h5v-5M20 20l-6-6",
  collapse: "M10 4v5H5M10 9L4 3M14 20v-5h5M14 15l6 6",
};

const FILL: Record<string, string> = {
  play: "M8 5v14l11-7z",
  skipStartTri: "M20 6l-9 6 9 6z",
  skipEndTri: "M4 6l9 6-9 6z",
};

export function Icon({
  name,
  size = 18,
  stroke = 1.6,
  color = "currentColor",
  style,
}: {
  name: IconName;
  size?: number;
  stroke?: number;
  color?: string;
  style?: CSSProperties;
}) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    xmlns: "http://www.w3.org/2000/svg",
    style: { display: "block", flexShrink: 0, ...style },
    "aria-hidden": true,
  } as const;

  if (name === "play") {
    return (
      <svg {...common}>
        <path d={FILL.play} fill={color} />
      </svg>
    );
  }
  if (name === "pause") {
    return (
      <svg {...common}>
        <rect x="7" y="5" width="3.5" height="14" rx="1" fill={color} />
        <rect x="13.5" y="5" width="3.5" height="14" rx="1" fill={color} />
      </svg>
    );
  }
  if (name === "skipStart") {
    return (
      <svg {...common}>
        <path d={STROKE.skipStart} stroke={color} strokeWidth={stroke} strokeLinecap="round" />
        <path d={FILL.skipStartTri} fill={color} />
      </svg>
    );
  }
  if (name === "skipEnd") {
    return (
      <svg {...common}>
        <path d={STROKE.skipEnd} stroke={color} strokeWidth={stroke} strokeLinecap="round" />
        <path d={FILL.skipEndTri} fill={color} />
      </svg>
    );
  }
  if (name === "lock") {
    return (
      <svg {...common}>
        <rect x="5" y="10" width="14" height="10" rx="2" stroke={color} strokeWidth={stroke} />
        <path d="M8 10V7a4 4 0 018 0v3" stroke={color} strokeWidth={stroke} strokeLinecap="round" />
      </svg>
    );
  }
  if (name === "checkSquare") {
    return (
      <svg {...common}>
        <rect x="3.5" y="3.5" width="17" height="17" rx="4" stroke={color} strokeWidth={stroke} />
        <path d="M8 12l3 3 5-5.5" stroke={color} strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (name === "alert") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="9" stroke={color} strokeWidth={stroke} fill="none" />
        <path d="M12 7v6M12 16.5v.5" stroke={color} strokeWidth={1.8} strokeLinecap="round" />
      </svg>
    );
  }
  if (name === "branch") {
    return (
      <svg {...common}>
        <path d="M6 3v12" stroke={color} strokeWidth={stroke} strokeLinecap="round" />
        <circle cx="18" cy="6" r="3" stroke={color} strokeWidth={stroke} fill="none" />
        <circle cx="6" cy="18" r="3" stroke={color} strokeWidth={stroke} fill="none" />
        <path d="M18 9a9 9 0 01-9 9" stroke={color} strokeWidth={stroke} strokeLinecap="round" fill="none" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <path
        d={STROKE[name]}
        stroke={color}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
