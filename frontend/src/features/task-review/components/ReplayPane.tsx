import { useEffect, useRef, useState } from "react";
import { Icon, t, weight, ACTION_COLOR } from "../../../ds";
import type { Step, Tab } from "../../../lib/types";

function TabStrip({ tabs, activeId, onSelect }: { tabs: Tab[]; activeId: string; onSelect: (id: string) => void }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 3, padding: "7px 8px 0", background: t.n8, borderBottom: `1px solid ${t.n7}` }}>
      {tabs.map((tab) => {
        const active = tab.id === activeId;
        return (
          <div
            key={tab.id}
            onClick={() => onSelect(tab.id)}
            style={{
              display: "flex", alignItems: "center", gap: 8, height: 32, padding: "0 12px",
              borderRadius: "8px 8px 0 0", cursor: "pointer", transition: t.transitionUi,
              background: active ? t.n9 : "transparent",
              borderTop: active ? `2px solid ${tab.color}` : "2px solid transparent",
              borderLeft: active ? `1px solid ${t.n7}` : "1px solid transparent",
              borderRight: active ? `1px solid ${t.n7}` : "1px solid transparent",
              color: active ? t.n0 : t.n3, marginBottom: active ? -1 : 0,
            }}
          >
            <span style={{ width: 8, height: 8, borderRadius: t.radiusFull, background: tab.color, flexShrink: 0 }} />
            <span style={{ fontSize: "0.78rem", fontWeight: weight.semibold, maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{tab.title}</span>
            <Icon name="close" size={12} stroke={1.3} style={{ opacity: 0.4 }} />
          </div>
        );
      })}
      <span style={{ width: 28, height: 30, display: "inline-flex", alignItems: "center", justifyContent: "center", color: t.n3, cursor: "pointer" }}>
        <Icon name="plus" size={15} stroke={1.7} />
      </span>
    </div>
  );
}

function UrlBar({ host }: { host: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "9px 14px", borderBottom: `1px solid ${t.n7}`, background: t.n9 }}>
      <div style={{ display: "flex", gap: 4, color: t.n3 }}>
        <Icon name="chevronLeft" size={17} stroke={1.7} />
        <Icon name="chevronRight" size={17} stroke={1.7} style={{ opacity: 0.4 }} />
        <Icon name="reload" size={16} />
      </div>
      <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 8, height: 32, padding: "0 12px", background: t.n85, border: `1px solid ${t.n7}`, borderRadius: t.radius2xl }}>
        <Icon name="lock" size={13} stroke={1.8} color={t.green} />
        <span style={{ fontFamily: t.fontMono, fontSize: "0.78rem", color: t.n1 }}>{host}</span>
      </div>
      <span style={{ fontSize: "0.6875rem", fontWeight: weight.bold, letterSpacing: "0.06em", textTransform: "uppercase", color: t.n3 }}>Replay</span>
    </div>
  );
}

/** Inline correction editor — replaces the step card in place (spec §2.1b). */
function CorrectionEditor({ stepNumber, seed, onCancel, onSave }: { stepNumber: number; seed: string; onCancel: () => void; onSave: (text: string) => void }) {
  const [text, setText] = useState(seed);
  useEffect(() => setText(seed), [seed]);
  return (
    <div style={{ padding: 14, background: t.n9, border: `1px solid ${t.primary6}`, borderRadius: 10, boxShadow: t.shadowSm }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <Icon name="pencil" size={16} color={t.primary6} />
        <span style={{ fontSize: "0.84rem", fontWeight: weight.bold, color: t.n0 }}>Correct step {stepNumber}</span>
        <span style={{ fontSize: "0.75rem", color: t.n3 }}>Edit the action; the agent re-runs from this state.</span>
      </div>
      <textarea
        value={text} onChange={(e) => setText(e.target.value)} autoFocus
        style={{ width: "100%", boxSizing: "border-box", minHeight: 52, resize: "none", padding: "9px 12px", border: `1px solid ${t.primary6}`, borderRadius: t.radiusLg, fontFamily: t.fontPrimary, fontSize: "0.8125rem", lineHeight: 1.5, color: t.n0, outline: "none" }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8 }}>
        <span style={{ flex: 1, fontSize: "0.72rem", color: t.n3 }}>Steps after this point are discarded and re-generated.</span>
        <Pill onClick={onCancel} bg={t.n9} border={t.n6} color={t.n1}>Cancel</Pill>
        <Pill onClick={() => onSave(text)} bg={t.primary6} border={t.primary6} color={t.n9}>Re-run from step {stepNumber}</Pill>
      </div>
    </div>
  );
}

function Pill({ children, onClick, bg, border, color, leading }: { children: React.ReactNode; onClick: () => void; bg: string; border: string; color: string; leading?: React.ReactNode }) {
  return (
    <span onClick={onClick} style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "7px 12px", borderRadius: 7, border: `1px solid ${border}`, background: bg, color, fontSize: "0.8125rem", fontWeight: weight.semibold, cursor: "pointer", whiteSpace: "nowrap" }}>
      {leading}
      {children}
    </span>
  );
}

function StepCard({ step, stepNumber, resolved, verified, onVerify, onCorrect }: { step: Step; stepNumber: number; resolved: boolean; verified: boolean; onVerify: () => void; onCorrect?: () => void }) {
  const isError = step.type === "error";
  const color = ACTION_COLOR[step.type];
  const pink = ACTION_COLOR.tab;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 14px", background: t.n9, border: `1px solid ${t.n7}`, borderLeft: `3px solid ${isError ? t.red : color}`, borderRadius: 10, boxShadow: t.shadowSm }}>
      <span style={{ width: 9, height: 9, borderRadius: t.radiusFull, background: isError ? t.red : color, flexShrink: 0 }} />
      <span style={{ fontFamily: t.fontMono, fontSize: "0.6875rem", fontWeight: weight.bold, letterSpacing: "0.05em", color: t.n3, textTransform: "uppercase", whiteSpace: "nowrap" }}>
        Step {stepNumber} · {step.type}
      </span>
      <span style={{ flex: 1, minWidth: 0, fontSize: "0.84rem", fontWeight: weight.semibold, color: t.n0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{step.description}</span>
      {/* A re-run step is MARKED as such (provenance) but stays fully reviewable —
          the annotator must be able to verify + correct the new steps to iterate
          the agent toward the target across rounds. */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
        {resolved && (
          <span style={{ fontSize: "0.6875rem", fontWeight: weight.bold, textTransform: "uppercase", letterSpacing: "0.04em", color: pink, background: `color-mix(in srgb, ${pink} 12%, transparent)`, padding: "5px 10px", borderRadius: t.radiusMd, whiteSpace: "nowrap" }}>Re-run</span>
        )}
        {verified ? (
          <Pill onClick={onVerify} bg={t.greenLite} border={`color-mix(in srgb, ${t.green} 45%, ${t.n9})`} color={t.greenDark} leading={<Icon name="check" size={14} stroke={2.4} color={t.greenDark} />}>Verified</Pill>
        ) : (
          <Pill onClick={onVerify} bg={t.n9} border={t.n6} color={t.n1} leading={<Icon name="check" size={14} stroke={2} />}>Verify</Pill>
        )}
        {/* Absent — not disabled — on an attempt with a version graph: this
            correction lands as a branch the version graph cannot see, so the
            golden it produces would contradict the annotator's own verdicts. */}
        {onCorrect && (
          <Pill onClick={onCorrect} bg={`color-mix(in srgb, ${t.primary6} 10%, transparent)`} border={`color-mix(in srgb, ${t.primary6} 25%, transparent)`} color={t.primary6} leading={<Icon name="pencil" size={13} />}>Correct</Pill>
        )}
      </div>
    </div>
  );
}

/** Transport bar — the 4th band inside the replay card (spec §2.1 / §2.2). */
function TransportBar({ steps, stepIndex, playing, onPlayToggle, onStepTo }: { steps: Step[]; stepIndex: number; playing: boolean; onPlayToggle: () => void; onStepTo: (i: number) => void }) {
  const total = steps.length;
  const IconBtn = ({ name, onClick }: { name: "skipStart" | "skipEnd"; onClick: () => void }) => (
    <span onClick={onClick} style={{ width: 32, height: 32, borderRadius: t.radiusMd, display: "inline-flex", alignItems: "center", justifyContent: "center", color: t.n3, cursor: "pointer" }}>
      <Icon name={name} size={16} />
    </span>
  );
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "11px 16px", borderTop: `1px solid ${t.n7}`, background: t.n9, flexShrink: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
        <IconBtn name="skipStart" onClick={() => onStepTo(Math.max(0, stepIndex - 1))} />
        <span onClick={onPlayToggle} style={{ width: 38, height: 38, borderRadius: t.radiusFull, background: t.primary6, color: t.n9, display: "inline-flex", alignItems: "center", justifyContent: "center", cursor: "pointer", flexShrink: 0 }}>
          <Icon name={playing ? "pause" : "play"} size={16} color={t.n9} />
        </span>
        <IconBtn name="skipEnd" onClick={() => onStepTo(Math.min(total - 1, stepIndex + 1))} />
      </div>
      <span style={{ fontFamily: t.fontMono, fontSize: "0.78rem", fontWeight: weight.bold, color: t.n1, whiteSpace: "nowrap", flexShrink: 0 }}>{stepIndex + 1} / {total}</span>
      <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 3, height: 20 }}>
        {steps.map((s, i) => {
          const color = ACTION_COLOR[s.type];
          const current = i === stepIndex;
          const played = i < stepIndex;
          return (
            <span
              key={s.idx}
              onClick={() => onStepTo(i)}
              style={{
                flex: 1, alignSelf: "center", height: current ? 18 : 9, borderRadius: 3, cursor: "pointer", transition: t.transitionUi,
                background: current ? color : played ? `color-mix(in srgb, ${color} 50%, ${t.n7})` : t.n6,
              }}
            />
          );
        })}
      </div>
    </div>
  );
}

export function ReplayPane({
  tabs, activeTabId, onSelectTab, step, stepNumber, stepIndex, steps, playing,
  resolved, verified, correcting, correctionSeed,
  onVerify, onStartCorrect, onCancelCorrect, onSaveCorrect, onPlayToggle, onStepTo,
}: {
  tabs: Tab[];
  activeTabId: string;
  onSelectTab: (id: string) => void;
  step: Step;
  stepNumber: number;
  stepIndex: number;
  steps: Step[];
  playing: boolean;
  resolved: boolean;
  verified: boolean;
  correcting: boolean;
  correctionSeed: string;
  onVerify: () => void;
  /** The LEGACY per-step correction. Undefined retires it for this attempt. */
  onStartCorrect?: () => void;
  onCancelCorrect: () => void;
  onSaveCorrect: (text: string) => void;
  onPlayToggle: () => void;
  onStepTo: (i: number) => void;
}) {
  const activeTab = tabs.find((tb) => tb.id === activeTabId) ?? tabs[0];
  const showOverlay = step.tabId === activeTabId;
  // The frame shown follows the SELECTED tab, not the current step — otherwise
  // every tab renders the current step's page (which was the bug). Use the most
  // recent step in the active tab at/before the current position; else that
  // tab's first frame; else the current step.
  const activeFrame = (() => {
    for (let i = Math.min(stepIndex, steps.length - 1); i >= 0; i--) {
      if (steps[i].tabId === activeTabId) return steps[i];
    }
    return steps.find((s) => s.tabId === activeTabId) ?? step;
  })();
  // Scale a captured snapshot (a full webpage) to FILL the frame width — the page
  // renders big and edge-to-edge, and scrolls vertically inside the frame exactly
  // like a real browser tab. (Fitting both dims shrank tall pages to a tiny,
  // side-gapped thumbnail.) Never upscale past 1:1 so text stays crisp.
  const boxRef = useRef<HTMLDivElement>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [fit, setFit] = useState({ scale: 1, w: 1280, h: 900 });
  const measure = () => {
    const box = boxRef.current;
    if (!box) return;
    const doc = iframeRef.current?.contentDocument;
    const w = Math.max(doc?.documentElement?.scrollWidth ?? 0, doc?.body?.scrollWidth ?? 0, 1200);
    const h = Math.max(doc?.documentElement?.scrollHeight ?? 0, doc?.body?.scrollHeight ?? 0, 1);
    setFit({ scale: Math.min(box.clientWidth / w, 1), w, h });
  };
  useEffect(() => {
    const onResize = () => measure();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Re-fit whenever the shown frame changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { measure(); }, [activeFrame.snapshot, activeFrame.image, activeTabId]);
  return (
    <div style={{ flex: 1, minHeight: 0, background: t.n9, border: `1px solid ${t.n7}`, borderRadius: t.radiusXl, boxShadow: t.shadowMd, overflow: "hidden", display: "flex", flexDirection: "column" }}>
      <TabStrip tabs={tabs} activeId={activeTabId} onSelect={onSelectTab} />
      <UrlBar host={activeTab.host} />
      <div style={{ position: "relative", flex: 1, minHeight: 0, overflow: "hidden", background: t.n85, display: "flex", flexDirection: "column" }}>
        <div style={{ padding: "9px 20px 2px", flexShrink: 0, display: "flex", alignItems: "baseline", gap: 10 }}>
          <div style={{ fontSize: "0.9375rem", fontWeight: weight.bold, color: t.n0, letterSpacing: "-0.3px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{activeTab.title}</div>
          <div style={{ fontSize: "0.72rem", color: t.n3, flexShrink: 0, whiteSpace: "nowrap" }}>captured frame</div>
        </div>
        <div style={{ position: "relative", flex: 1, minHeight: 0, marginTop: 8 }}>
          {activeFrame.image ? (
            // Gym captures are a FIXED-viewport screenshot (1280×800, full_page off).
            // Fit the WHOLE frame inside the box so the annotator always sees the
            // entire captured screen at once — no vertical scrolling, and nothing
            // (like the cart footer) gets cut off at the bottom.
            <div style={{ position: "absolute", inset: 0, background: t.n9, display: "flex", alignItems: "center", justifyContent: "center", padding: 6, overflow: "hidden" }}>
              <img src={activeFrame.image} alt="captured frame" style={{ display: "block", maxWidth: "100%", maxHeight: "100%", objectFit: "contain", borderRadius: 2 }} />
            </div>
          ) : activeFrame.snapshot ? (
            // Fixture DOM snapshots can be full-page (tall) — those still fill the
            // width and scroll vertically inside this box, like a browser tab.
            <div ref={boxRef} style={{ position: "absolute", inset: 0, overflowY: "auto", overflowX: "hidden", overscrollBehavior: "contain", background: t.n9 }}>
              <div style={{ width: fit.w * fit.scale, height: fit.h * fit.scale, position: "relative" }}>
                <iframe
                  ref={iframeRef}
                  key={activeFrame.snapshot}
                  title="captured frame"
                  src={`/api/snapshots/${activeFrame.snapshot}`}
                  sandbox="allow-same-origin"
                  onLoad={measure}
                  // read-only replay: make the frame pointer-transparent so wheel
                  // scrolls the surrounding box (a scaled iframe otherwise swallows
                  // the wheel and nothing scrolls).
                  style={{ position: "absolute", top: 0, left: 0, width: fit.w, height: fit.h, transform: `scale(${fit.scale})`, transformOrigin: "top left", border: "none", background: t.n9, pointerEvents: "none" }}
                />
              </div>
            </div>
          ) : null}
        </div>
        {/* Step controls live in their OWN row BELOW the page — never overlapping
            the captured frame, so the annotator sees the full screenshot. */}
        {showOverlay && (
          <div style={{ flexShrink: 0, padding: "8px 14px 12px", display: "flex", flexDirection: "column", gap: 8, background: t.n85, borderTop: `1px solid ${t.n7}` }}>
            {step.type === "error" && step.errorMsg && !correcting && (
              <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", background: t.redLite, border: `1px solid color-mix(in srgb, ${t.red} 42%, ${t.n9})`, borderRadius: 8, color: t.redDark }}>
                <Icon name="alert" size={17} stroke={1.7} color={t.redDark} />
                <span style={{ fontSize: "0.8125rem", fontWeight: weight.semibold }}>{step.errorMsg}</span>
              </div>
            )}
            {correcting ? (
              <CorrectionEditor stepNumber={stepNumber} seed={correctionSeed} onCancel={onCancelCorrect} onSave={onSaveCorrect} />
            ) : (
              <StepCard step={step} stepNumber={stepNumber} resolved={resolved} verified={verified} onVerify={onVerify} onCorrect={onStartCorrect} />
            )}
          </div>
        )}
      </div>
      <TransportBar steps={steps} stepIndex={stepIndex} playing={playing} onPlayToggle={onPlayToggle} onStepTo={onStepTo} />
    </div>
  );
}
