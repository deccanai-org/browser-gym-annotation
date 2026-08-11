/**
 * Typed references to the Deccan AI Experts design tokens.
 * Components read colors/spacing/etc. through `t` (CSS `var(--…)` strings)
 * so we never hardcode hex and stay adherence-clean. Values live in
 * src/styles/tokens/*.css (the vendored DS).
 */
export const t = {
  // Primary — the DS warm ramp; every interactive action.
  // 6 is the CTA, 7 the hover, 8 the pressed state.
  primary0: "var(--primary-0)",
  primary6: "var(--primary-6)",
  primary7: "var(--primary-7)",
  primary8: "var(--primary-8)",

  // Heading reds — the DS colours titles apart from CTAs
  headingRed: "var(--heading-red)",
  headingOrange: "var(--heading-orange)",

  // Neutrals: 0=text … 9=white. Warm ink, not grey — the DS has no
  // grey fills, so the light end runs through its warm tints.
  n0: "var(--neutrals-0)",
  n1: "var(--neutrals-1)",
  n2: "var(--neutrals-2)",
  n3: "var(--neutrals-3)",
  n4: "var(--neutrals-4)",
  n5: "var(--neutrals-5)",
  n6: "var(--neutrals-6)",
  n7: "var(--neutrals-7)",
  n8: "var(--neutrals-8)",
  n85: "var(--neutrals-85)",
  n9: "var(--neutrals-9)",

  // Semantic accents (base + lite/dark used for chips)
  red: "var(--accent-red)",
  redLite: "var(--accent-red-lite)",
  redDark: "var(--accent-red-dark)",
  green: "var(--accent-green)",
  greenLite: "var(--accent-green-lite)",
  greenDark: "var(--accent-green-dark)",
  yellow: "var(--accent-yellow)",
  yellowDark: "var(--accent-yellow-dark)",
  purple: "var(--accent-purple)",

  // Delta / categorical hues (timeline + tags). The DS only ever needs
  // four; these extend its ramp with the spot-illustration palette so
  // seven action types stay tellable apart. Key names are legacy — the
  // hue behind each is warm, not the colour the name suggests.
  deltaPink: "var(--delta-pink)",
  deltaCyan: "var(--delta-cyan)",
  deltaViolet: "var(--delta-violet)",
  deltaBlue: "var(--delta-blue)",
  deltaEmerald: "var(--delta-emerald)",
  deltaAmber: "var(--delta-amber)",
  deltaRose: "var(--delta-rose)",
  deltaTagId: "var(--delta-tag-id)",

  // Surfaces / borders / text (semantic aliases)
  surfacePage: "var(--surface-page)",
  surfaceCard: "var(--surface-card)",
  surfaceAlt: "var(--surface-alt)",
  surfaceTint: "var(--surface-tint)",
  borderCard: "var(--border-card)",
  borderHairline: "var(--border-hairline)",
  borderDashed: "var(--border-dashed)",
  textPrimary: "var(--text-primary)",
  textSecondary: "var(--text-secondary)",
  textMuted: "var(--text-muted)",
  textTitle: "var(--text-title)",

  // The wash. `bgWash` is the working-surface gradient; `bgWashFull` is
  // the full DS mesh, for hero surfaces only.
  bgWash: "var(--bg-wash)",
  bgWashFull: "var(--bg-wash-full)",
  assetWaves: "var(--asset-waves)",

  // Type
  fontPrimary: "var(--font-primary)",
  fontDisplay: "var(--font-display)",
  fontSerif: "var(--font-serif)",
  fontMono: "var(--font-mono)",
  trackingHeading: "var(--tracking-heading)",
  trackingEyebrow: "var(--tracking-eyebrow)",

  // Spacing / radius / shadows / motion
  radiusSm: "var(--radius-sm)",
  radiusMd: "var(--radius-md)",
  radiusLg: "var(--radius-lg)",
  radiusXl: "var(--radius-xl)",
  radius2xl: "var(--radius-2xl)",
  radius3xl: "var(--radius-3xl)",
  radiusPill: "var(--radius-pill)",
  radiusFull: "var(--radius-full)",
  shadowSm: "var(--shadow-sm)",
  shadowMd: "var(--shadow-md)",
  shadowLg: "var(--shadow-lg)",
  shadowXl: "var(--shadow-xl)",
  shadowCard: "var(--shadow-card)",
  shadowHover: "var(--shadow-hover)",
  shadowFocus: "var(--shadow-focus)",
  shadowElevated: "var(--shadow-elevated)",
  transitionUi: "var(--transition-ui)",
  transitionLayout: "var(--transition-layout)",
} as const;

/** Weights — Figtree is variable 300–900, so 600 is a real SemiBold. */
export const weight = {
  regular: 400,
  medium: 500,
  semibold: 600,
  bold: 700,
  black: 900,
} as const;

/** The 7 agent action types → their categorical hue (matches the design). */
export const ACTION_COLOR = {
  navigate: t.deltaBlue,
  type: t.deltaCyan,
  click: t.deltaViolet,
  submit: t.deltaEmerald,
  extract: t.deltaAmber,
  error: t.deltaRose,
  tab: t.deltaPink,
} as const;
export type ActionType = keyof typeof ACTION_COLOR;

/** The 5 verifier levels → dot hue + the type-chip label shown in the group card.
 *  Spread across the ramp rather than bunched at the orange end, so five dots
 *  in a column are still tellable apart. */
export const VERIFIER_LEVEL = {
  ui: { label: "UI State", chip: "DOM", color: t.deltaCyan },
  backend: { label: "Backend State", chip: "SQL", color: t.deltaBlue },
  semantic: { label: "Semantic", chip: "LLM judge", color: t.deltaPink },
  process: { label: "Process", chip: "Trace", color: t.deltaAmber },
  safety: { label: "Safety", chip: "Policy", color: t.deltaRose },
} as const;
export type VerifierLevel = keyof typeof VERIFIER_LEVEL;

/**
 * `color-mix` tint used by chips/badges (e.g. a 12%-of-hue fill).
 * A helper so components don't hand-write color-mix strings.
 */
export function tint(color: string, pct: number): string {
  return `color-mix(in srgb, ${color} ${pct}%, transparent)`;
}

/** Avatar fills, in ramp order. Gold is deliberately absent: initials are set
 *  in white and gold cannot carry them. */
const AVATAR_FILL = [t.red, t.deltaCyan, t.primary6, t.deltaBlue, t.deltaPink, t.green];

/**
 * An annotator's avatar colour. The API hands out a free 0–360 hue, which in a
 * palette with no cool colours would put blue and green faces in the header —
 * so the hue only picks a stop on the DS ramp, it never becomes an hsl().
 */
export function avatarColor(hue: number): string {
  const i = Math.abs(Math.round(hue)) % AVATAR_FILL.length;
  return AVATAR_FILL[i];
}
