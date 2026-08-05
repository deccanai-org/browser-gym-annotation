import { FocusBadge, Icon, t, weight } from "../../../ds";
import type { Annotator } from "../../auth/authApi";

function Rule() {
  return <span style={{ width: 1, height: 22, background: t.n7, flexShrink: 0 }} />;
}

function PagerBox({ dir, onClick, disabled }: { dir: "chevronLeft" | "chevronRight"; onClick: () => void; disabled: boolean }) {
  return (
    <span
      onClick={disabled ? undefined : onClick}
      title={dir === "chevronLeft" ? "Previous task" : "Next task"}
      style={{
        width: 30,
        height: 30,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        borderRadius: 7,
        border: `1px solid ${t.n6}`,
        background: t.n9,
        color: t.n2,
        cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.4 : 1,
      }}
    >
      <Icon name={dir} size={16} stroke={1.8} />
    </span>
  );
}

export function Header({ index, total, onPrev, onNext, onSkip, onBrowseGym, gymTaskId, gymAdhoc, onExitGym, onOpenQa, annotator, onOpenProfile, queueSet, onToggleQueue, onBackToTasks }: { index: number; total: number; onPrev: () => void; onNext: () => void; onSkip: () => void; onBrowseGym: () => void; gymTaskId?: string | null; gymAdhoc?: boolean; onExitGym?: () => void; onOpenQa?: () => void; annotator?: Annotator | null; onOpenProfile?: () => void; queueSet?: "breakers" | "fixtures"; onToggleQueue?: () => void; onBackToTasks?: () => void }) {
  const mono = { fontFamily: t.fontMono } as const;
  const name = annotator?.displayName || annotator?.email || "?";
  const initial = name.trim().charAt(0).toUpperCase() || "?";
  return (
    <header
      style={{
        position: "sticky",
        top: 0,
        zIndex: 20,
        height: 56,
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "0 20px",
        background: t.n9,
        borderBottom: `1px solid ${t.n7}`,
      }}
    >
      <img src="/deccan-ai-wordmark.svg" alt="Deccan AI" style={{ height: 22, width: "auto" }} />
      <Rule />
      <nav style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "0.8125rem" }}>
        <span style={{ color: t.n3 }}>Browser-Use Gym</span>
        <Icon name="chevronRight" size={14} stroke={1.6} color={t.n3} />
        {onBackToTasks ? (
          // Opened from the board — the crumb goes back to it, so leaving a task
          // is one click and the annotator never gets stranded on a single task.
          <span onClick={onBackToTasks} style={{ color: t.n3, cursor: "pointer" }}>My tasks</span>
        ) : (
          <span style={{ color: t.n1, fontWeight: weight.semibold }}>Tasking</span>
        )}
        {onBackToTasks && <Icon name="chevronRight" size={14} stroke={1.6} color={t.n3} />}
        {onBackToTasks && <span style={{ color: t.n1, fontWeight: weight.semibold }}>Task</span>}
      </nav>
      <Rule />
      {gymTaskId && gymAdhoc ? (
        // An off-queue task loaded ad-hoc via the Gym picker — not part of the queue.
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: "0.8125rem", fontWeight: weight.semibold, color: t.n1, whiteSpace: "nowrap" }}>
            Gym · <span style={mono}>{gymTaskId}</span>
          </span>
          <span onClick={onExitGym} style={{ marginLeft: 4, fontSize: "0.78125rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer" }}>Back to queue</span>
        </div>
      ) : (
        // The main queue — breakers or demo fixtures — navigated by the pager.
        <div style={{ display: "flex", alignItems: "center", gap: 8 }} title="One task at a time">
          <PagerBox dir="chevronLeft" onClick={onPrev} disabled={index <= 0} />
          <span style={{ fontSize: "0.8125rem", fontWeight: weight.semibold, color: t.n1, whiteSpace: "nowrap" }}>
            {queueSet === "fixtures" ? "Demo" : "Breaker"} <span style={mono}>{index + 1}</span> of <span style={mono}>{total}</span>
          </span>
          <PagerBox dir="chevronRight" onClick={onNext} disabled={index >= total - 1} />
          <span onClick={onSkip} style={{ marginLeft: 4, fontSize: "0.78125rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer" }}>Skip</span>
        </div>
      )}
      <span style={{ width: 1, height: 22, background: t.n7 }} />
      {onToggleQueue && (
        <span onClick={onToggleQueue} title="Switch between the breaker queue and the demo fixtures" style={{ fontSize: "0.78125rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer", whiteSpace: "nowrap" }}>
          {queueSet === "breakers" ? "Demos" : "Breakers"}
        </span>
      )}
      <span onClick={onBrowseGym} style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: "0.78125rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer", whiteSpace: "nowrap" }}>
        <Icon name="swap" size={14} /> All gym tasks
      </span>
      {onOpenQa && (
        <span onClick={onOpenQa} title="Multi-annotator QA — agreement + adjudication" style={{ fontSize: "0.78125rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer", whiteSpace: "nowrap" }}>
          ⚖ QA review
        </span>
      )}
      <span style={{ flex: 1 }} />
      <FocusBadge>Multitab · Web Navigation</FocusBadge>
      <span
        onClick={onOpenProfile}
        title="Your profile — view stats or log out"
        style={{ display: "inline-flex", alignItems: "center", gap: 9, cursor: "pointer", padding: "4px 8px 4px 4px", borderRadius: t.radiusFull, border: `1px solid ${t.n7}` }}
      >
        <span style={{ width: 32, height: 32, borderRadius: t.radiusFull, background: annotator ? `hsl(${annotator.avatarHue} 62% 52%)` : t.primary7, color: "#fff", display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: "0.8rem", fontWeight: weight.bold, flexShrink: 0 }}>{initial}</span>
        <span style={{ display: "inline-flex", flexDirection: "column", lineHeight: 1.15, marginRight: 2 }}>
          <span style={{ fontSize: "0.78rem", fontWeight: weight.semibold, color: t.n1 }}>{name}</span>
          <span style={{ fontSize: "0.62rem", color: t.n3, textTransform: "uppercase", letterSpacing: "0.05em" }}>{annotator?.role ?? ""}</span>
        </span>
      </span>
    </header>
  );
}
