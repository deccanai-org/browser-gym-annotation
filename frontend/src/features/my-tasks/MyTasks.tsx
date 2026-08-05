/**
 * The board the annotator lands on after signing in.
 *
 * The queue as a PERSON sees it — their breakers, each tagged with where they
 * left off — not a flat catalogue. Picking one opens the live-browser annotation
 * screen for that task; everything here is about choosing what to work on and
 * seeing progress, so it is read-only and quiet.
 */
import { useEffect, useMemo, useState } from "react";
import { Icon, t, weight } from "../../ds";
import { APP_COLOR } from "../../lib/appColors";
import type { AppKey } from "../../lib/types";
import { fetchMyTasks, type MyTaskRow, type MyTaskStatus, type MyTasksBoard } from "../../lib/api";
import { useAuth } from "../auth/AuthContext";
import { ProfilePanel } from "../auth/ProfilePanel";

const STATUS: Record<MyTaskStatus, { label: string; fg: string; bg: string }> = {
  todo: { label: "To do", fg: t.n2, bg: t.n7 },
  in_progress: { label: "In progress", fg: t.primary6, bg: t.primary0 },
  returned: { label: "Returned", fg: t.yellowDark, bg: t.yellow },
  in_review: { label: "In review", fg: t.purple, bg: t.n7 },
  submitted: { label: "Submitted", fg: t.greenDark, bg: t.greenLite },
};

type Filter = "todo" | "in_progress" | "returned" | "in_review" | "submitted" | "all";
const FILTERS: { key: Filter; label: string }[] = [
  { key: "todo", label: "To do" },
  { key: "in_progress", label: "In progress" },
  { key: "returned", label: "Returned" },
  { key: "in_review", label: "In review" },
  { key: "submitted", label: "Submitted" },
  { key: "all", label: "All" },
];

function StatusPill({ status }: { status: MyTaskStatus }) {
  const s = STATUS[status];
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5, padding: "3px 9px",
      borderRadius: t.radiusPill, background: s.bg, color: s.fg,
      fontSize: "0.72rem", fontWeight: weight.semibold, whiteSpace: "nowrap",
    }}>
      <span aria-hidden style={{ width: 6, height: 6, borderRadius: 999, background: s.fg }} />
      {s.label}
    </span>
  );
}

function Sites({ row }: { row: MyTaskRow }) {
  const [first, ...rest] = row.sites;
  if (!first) return <span style={{ color: t.n4 }}>—</span>;
  const hue = APP_COLOR[first.app as AppKey] ?? t.n4;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: "0.8rem", fontFamily: t.fontMono, color: t.n2 }}>
      <span aria-hidden style={{ width: 7, height: 7, borderRadius: 999, background: hue, flexShrink: 0 }} />
      {first.domain}
      {rest.length > 0 && <span style={{ color: t.n4 }}>+{rest.length}</span>}
    </span>
  );
}

function StartButton({ row, onOpen }: { row: MyTaskRow; onOpen: (id: string) => void }) {
  const resuming = row.status === "in_progress";
  const done = row.status === "submitted";
  return (
    <button
      onClick={() => onOpen(row.id)}
      style={{
        display: "inline-flex", alignItems: "center", gap: 6, padding: "8px 16px",
        borderRadius: t.radiusMd, border: "none", cursor: "pointer", whiteSpace: "nowrap",
        background: done ? t.n7 : t.greenDark, color: done ? t.n1 : "#fff",
        fontSize: "0.8rem", fontWeight: weight.semibold,
      }}
    >
      {!done && <Icon name="play" size={12} stroke={2.4} />}
      {done ? "Review" : resuming ? "Continue" : "Start"}
    </button>
  );
}

/** The primary call-to-action: the one task most naturally resumed. */
function NextUp({ row, onOpen }: { row: MyTaskRow; onOpen: (id: string) => void }) {
  const resume = row.resumeStep != null ? ` · resume at step ${row.resumeStep}` : "";
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 20, padding: "18px 22px",
      border: `1px solid ${t.primary7}`, borderRadius: t.radiusXl,
      background: t.primary0, marginBottom: 26,
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
          <span style={{ fontSize: "0.68rem", fontWeight: weight.bold, letterSpacing: "0.08em", color: t.primary6 }}>NEXT UP</span>
          <span style={{ fontFamily: t.fontMono, fontSize: "0.72rem", color: t.n2, background: t.n9, padding: "2px 8px", borderRadius: t.radiusSm, border: `1px solid ${t.n7}` }}>{row.id}</span>
          <StatusPill status={row.status} />
        </div>
        <div style={{ fontSize: "1.05rem", fontWeight: weight.bold, color: t.n0, marginBottom: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {row.title}
        </div>
        <div style={{ fontSize: "0.8rem", color: t.n3 }}>
          {row.category || "Breaker"} · {row.sites.map((s) => s.title).join(", ")}{resume}
        </div>
      </div>
      <StartButton row={row} onOpen={onOpen} />
    </div>
  );
}

function QuotaCard({ board }: { board: MyTasksBoard }) {
  const { submitted, target } = board.quota;
  const pct = target > 0 ? Math.round((submitted / target) * 100) : 0;
  return (
    <div style={{
      minWidth: 260, padding: "14px 18px", border: `1px solid ${t.n7}`,
      borderRadius: t.radiusLg, background: t.n9,
    }}>
      <div style={{ fontSize: "0.66rem", fontWeight: weight.bold, letterSpacing: "0.08em", color: t.n3, marginBottom: 8 }}>
        SUBMITTED
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 6, marginBottom: 10 }}>
        <span style={{ fontSize: "1.5rem", fontWeight: weight.bold, color: t.n0, fontVariantNumeric: "tabular-nums" }}>{submitted}</span>
        <span style={{ fontSize: "0.9rem", color: t.n3, fontVariantNumeric: "tabular-nums" }}>/ {target}</span>
      </div>
      <div style={{ height: 6, borderRadius: 999, background: t.n7, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: t.greenDark, borderRadius: 999, transition: "width .3s" }} />
      </div>
    </div>
  );
}

export function MyTasks({ onOpenTask }: { onOpenTask: (taskId: string) => void }) {
  const { annotator } = useAuth();
  const [board, setBoard] = useState<MyTasksBoard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [filter, setFilter] = useState<Filter>("todo");
  const [profileOpen, setProfileOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchMyTasks().then((b) => {
      if (!alive) return;
      if (b) { setBoard(b); setError(false); } else setError(true);
      setLoading(false);
    });
    return () => { alive = false; };
  }, []);

  const rows = useMemo(() => {
    if (!board) return [];
    return filter === "all" ? board.tasks : board.tasks.filter((r) => r.status === filter);
  }, [board, filter]);

  const name = annotator?.displayName || annotator?.email || "?";
  const initial = name.trim().charAt(0).toUpperCase() || "?";

  return (
    <div style={{ minHeight: "100vh", background: t.surfacePage, fontFamily: t.fontPrimary, display: "flex", flexDirection: "column" }}>
      {/* Header — same chrome as the annotation screen, so signing in and moving
          between the board and a task feels like one app. */}
      <header style={{ position: "sticky", top: 0, zIndex: 20, height: 56, flexShrink: 0, display: "flex", alignItems: "center", gap: 16, padding: "0 20px", background: t.n9, borderBottom: `1px solid ${t.n7}` }}>
        <img src="/deccan-ai-wordmark.svg" alt="Deccan AI" style={{ height: 22, width: "auto" }} />
        <span style={{ width: 1, height: 22, background: t.n7 }} />
        <nav style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "0.8125rem" }}>
          <span style={{ color: t.n3 }}>Browser-Use Gym</span>
          <Icon name="chevronRight" size={14} stroke={1.6} color={t.n3} />
          <span style={{ color: t.n1, fontWeight: weight.semibold }}>My tasks</span>
        </nav>
        <div style={{ flex: 1 }} />
        <span style={{ padding: "6px 14px", borderRadius: t.radiusPill, border: `1px solid ${t.primary7}`, color: t.primary6, fontSize: "0.8rem", fontWeight: weight.semibold }}>
          Multitab · Web Navigation
        </span>
        <button onClick={() => setProfileOpen(true)} title={name} style={{ width: 34, height: 34, borderRadius: 999, border: "none", cursor: "pointer", background: t.primary6, color: "#fff", fontWeight: weight.bold, fontSize: "0.85rem" }}>
          {initial}
        </button>
      </header>

      <main style={{ flex: 1, width: "100%", maxWidth: 1200, margin: "0 auto", padding: "32px 24px 48px" }}>
        {loading ? (
          <div style={{ padding: 80, textAlign: "center", color: t.n3 }}>Loading your tasks…</div>
        ) : error || !board ? (
          <div style={{ padding: 40, textAlign: "center", color: t.redDark, border: `1px solid ${t.n7}`, borderRadius: t.radiusLg, background: t.n9 }}>
            Could not load your tasks — the backend may be offline.
          </div>
        ) : (
          <>
            {/* Title + quota */}
            <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 20, marginBottom: 26, flexWrap: "wrap" }}>
              <div>
                <h1 style={{ margin: 0, fontSize: "1.7rem", fontWeight: weight.bold, color: t.n0 }}>My tasks</h1>
                <div style={{ marginTop: 6, fontSize: "0.85rem", color: t.n3 }}>
                  {board.assigned} curated breakers · batch <span style={{ fontFamily: t.fontMono, color: t.n2 }}>{board.batch}</span>
                </div>
              </div>
              <QuotaCard board={board} />
            </div>

            {board.nextUp && <NextUp row={board.nextUp} onOpen={onOpenTask} />}

            {/* Filter tabs */}
            <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 4, borderBottom: `1px solid ${t.n7}`, flexWrap: "wrap" }}>
              {FILTERS.map((f) => {
                const active = filter === f.key;
                const n = board.counts[f.key] ?? 0;
                return (
                  <button key={f.key} onClick={() => setFilter(f.key)}
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 7, padding: "9px 14px",
                      border: "none", background: "transparent", cursor: "pointer",
                      borderBottom: `2px solid ${active ? t.primary6 : "transparent"}`, marginBottom: -1,
                      color: active ? t.n0 : t.n3, fontSize: "0.82rem", fontWeight: active ? weight.semibold : weight.regular,
                    }}>
                    {f.label}
                    <span style={{ fontSize: "0.72rem", color: active ? t.primary6 : t.n4, fontVariantNumeric: "tabular-nums" }}>{n}</span>
                  </button>
                );
              })}
            </div>

            {/* Table */}
            <div style={{ border: `1px solid ${t.n7}`, borderTop: "none", borderRadius: `0 0 ${t.radiusLg} ${t.radiusLg}`, overflow: "hidden", background: t.n9 }}>
              <div style={{ display: "grid", gridTemplateColumns: "120px 1fr 130px 190px 110px 120px", gap: 12, padding: "11px 18px", borderBottom: `1px solid ${t.n7}`, background: t.n85, fontSize: "0.68rem", fontWeight: weight.bold, letterSpacing: "0.05em", color: t.n3 }}>
                <span>TASK ID</span><span>TITLE</span><span>STATUS</span><span>SITES</span><span>TYPE</span><span style={{ textAlign: "right" }}>ACTION</span>
              </div>
              {rows.length === 0 ? (
                <div style={{ padding: 40, textAlign: "center", color: t.n3, fontSize: "0.85rem" }}>
                  Nothing here yet.
                </div>
              ) : rows.map((r) => (
                <div key={r.id} style={{ display: "grid", gridTemplateColumns: "120px 1fr 130px 190px 110px 120px", gap: 12, padding: "14px 18px", borderBottom: `1px solid ${t.n8}`, alignItems: "center" }}>
                  <span style={{ fontFamily: t.fontMono, fontSize: "0.72rem", color: t.n2, background: t.n85, padding: "3px 8px", borderRadius: t.radiusSm, justifySelf: "start" }}>{r.id.split("/")[0]}</span>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: "0.9rem", fontWeight: weight.semibold, color: t.n0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.title}</div>
                    <div style={{ fontSize: "0.72rem", color: t.n3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {r.category || "Breaker"}{r.status === "in_progress" && r.resumeStep != null ? ` · resume at step ${r.resumeStep}` : ""}
                    </div>
                  </div>
                  <StatusPill status={r.status} />
                  <Sites row={r} />
                  <span style={{ fontSize: "0.76rem", color: t.n2, textTransform: "capitalize" }}>{r.difficulty || "—"}</span>
                  <span style={{ justifySelf: "end" }}><StartButton row={r} onOpen={onOpenTask} /></span>
                </div>
              ))}
            </div>

            <div style={{ marginTop: 12, display: "flex", justifyContent: "space-between", fontSize: "0.76rem", color: t.n3 }}>
              <span>Showing {rows.length} of {board.assigned} assigned tasks</span>
              <span>Your progress is saved per task.</span>
            </div>
          </>
        )}
      </main>

      {profileOpen && <ProfilePanel onClose={() => setProfileOpen(false)} />}
    </div>
  );
}

export default MyTasks;
