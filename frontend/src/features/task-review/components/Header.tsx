import { avatarColor, FocusBadge, Icon, t, weight } from "../../../ds";
import type { Annotator } from "../../auth/authApi";

function Rule() {
  return <span style={{ width: 1, height: 22, background: t.n7, flexShrink: 0 }} />;
}

export function Header({ onBrowseGym, gymTaskId, gymAdhoc, onExitGym, annotator, onOpenProfile, onBackToTasks }: {
  onBrowseGym: () => void;
  gymTaskId?: string | null;
  gymAdhoc?: boolean;
  onExitGym?: () => void;
  annotator?: Annotator | null;
  onOpenProfile?: () => void;
  onBackToTasks?: () => void;
}) {
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
          <span onClick={onExitGym} style={{ marginLeft: 4, fontSize: "0.78125rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer" }}>Back to my task</span>
        </div>
      ) : null /* One task, the one opened from the board — the breadcrumb above
                    already names it. There used to be a pager and a Demos toggle
                    here; Next/Prev walked an annotator straight off their assigned
                    task into someone else's queue position, which is exactly what
                    assignment exists to prevent. */}
      <span style={{ width: 1, height: 22, background: t.n7 }} />
      <span onClick={onBrowseGym} style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: "0.78125rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer", whiteSpace: "nowrap" }}>
        <Icon name="swap" size={14} /> All gym tasks
      </span>

      <span style={{ flex: 1 }} />
      <FocusBadge>Multitab · Web Navigation</FocusBadge>
      <span
        onClick={onOpenProfile}
        title="Your profile — view stats or log out"
        style={{ display: "inline-flex", alignItems: "center", gap: 9, cursor: "pointer", padding: "4px 8px 4px 4px", borderRadius: t.radiusFull, border: `1px solid ${t.n7}` }}
      >
        <span style={{ width: 32, height: 32, borderRadius: t.radiusFull, background: annotator ? avatarColor(annotator.avatarHue) : t.primary7, color: t.n9, display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: "0.8rem", fontWeight: weight.bold, flexShrink: 0 }}>{initial}</span>
        <span style={{ display: "inline-flex", flexDirection: "column", lineHeight: 1.15, marginRight: 2 }}>
          <span style={{ fontSize: "0.78rem", fontWeight: weight.semibold, color: t.n1 }}>{name}</span>
          <span style={{ fontSize: "0.62rem", color: t.n3, textTransform: "uppercase", letterSpacing: t.trackingEyebrow }}>{annotator?.role ?? ""}</span>
        </span>
      </span>
    </header>
  );
}
