/** Review other people's work — a screen, not a modal inside a task.
 *
 *  It used to live inside the annotation screen, so reaching it meant opening
 *  somebody's task, which boots a real Chromium and leases a gym from the pool.
 *  A reviewer paid that price to read a list.
 *
 *  Two verbs, not one. Accepting was all a reviewer could do: a sample they
 *  judged wrong could be left unaccepted, but nothing told the annotator and the
 *  board's "returned" tab could never fill. Returning it says so, in words the
 *  annotator reads, and the note is required — "do it again" with no reason is
 *  not a review.
 */
import { useEffect, useState } from "react";

import {
  adjudicate,
  downloadSampleBundle,
  fetchQaSubmissions,
  fetchQaTasks,
  returnSubmission,
  type QaSubmission,
  type QaTaskRow,
} from "../../lib/api";
import { Button, Icon, t, tint, weight } from "../../ds";

export function QaScreen({ onExit }: { onExit: () => void }) {
  const [tasks, setTasks] = useState<QaTaskRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [subs, setSubs] = useState<QaSubmission[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Which submission is being returned, and the reason. Kept per-submission so
  // opening the box on one row cannot post the note against another.
  const [returning, setReturning] = useState<string | null>(null);
  const [note, setNote] = useState("");

  const reload = () => fetchQaTasks().then(setTasks);
  useEffect(() => { void reload(); }, []);

  const openTask = async (id: string) => {
    setSelected(id);
    setSubs(null);
    setReturning(null);
    setError(null);
    const r = await fetchQaSubmissions(id);
    setSubs(r?.submissions ?? []);
  };

  const accept = async (sessionId: string) => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await adjudicate(selected, sessionId);
      await openTask(selected);
      await reload();
    } finally {
      setBusy(false);
    }
  };

  const sendBack = async (submissionId: string) => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await returnSubmission(submissionId, note);
      setReturning(null);
      setNote("");
      await openTask(selected);
      await reload();
    } catch (e) {
      // The server's own sentence — including "say why you are sending it back".
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const badge = (row: QaTaskRow) => {
    if (row.adjudicated) return { txt: "adjudicated", bg: tint(t.green, 14), fg: t.greenDark };
    if (row.disputed) return { txt: `disputed · ${Math.round((row.agreement ?? 0) * 100)}%`, bg: tint(t.yellow, 16), fg: t.n1 };
    return { txt: "unanimous", bg: t.n8, fg: t.n2 };
  };

  return (
    <div style={{ minHeight: "100vh", background: t.n85, display: "flex", flexDirection: "column" }}>
      <header style={{ height: 56, flexShrink: 0, display: "flex", alignItems: "center", gap: 14,
                       padding: "0 20px", background: t.n9, borderBottom: `1px solid ${t.n7}` }}>
        <Button variant="soft" onClick={onExit}>← My tasks</Button>
        <span style={{ fontWeight: weight.bold, fontSize: "0.95rem" }}>QA review</span>
        <span style={{ flex: 1 }} />
        <a href="/api/export/dataset.jsonl?accepted=true" download
           style={{ fontSize: "0.8rem", color: t.primary6, fontWeight: weight.semibold, textDecoration: "none" }}>
          ⬇ Export golden dataset
        </a>
      </header>

      <div style={{ flex: 1, display: "grid", gridTemplateColumns: "minmax(280px, 380px) 1fr", gap: 0, minHeight: 0 }}>
        {/* every task that has at least one submission */}
        <aside style={{ borderRight: `1px solid ${t.n7}`, overflowY: "auto", background: t.n9 }}>
          {tasks === null ? (
            <p style={{ padding: 20, color: t.n3, fontSize: "0.82rem" }}>Loading…</p>
          ) : tasks.length === 0 ? (
            <p style={{ padding: 20, color: t.n3, fontSize: "0.82rem" }}>
              Nothing has been submitted yet.
            </p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {tasks.map((row) => {
                const b = badge(row);
                const on = row.taskExternalId === selected;
                return (
                  <li key={row.taskExternalId}>
                    <button
                      type="button"
                      onClick={() => void openTask(row.taskExternalId)}
                      style={{ width: "100%", textAlign: "left", padding: "12px 16px", cursor: "pointer",
                               border: "none", borderBottom: `1px solid ${t.n8}`,
                               background: on ? tint(t.primary6, 8) : "transparent" }}
                    >
                      <span style={{ display: "block", fontSize: "0.82rem", fontWeight: weight.semibold }}>
                        {row.title || row.taskExternalId}
                      </span>
                      <span style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
                        <span style={{ fontSize: "0.68rem", padding: "2px 7px", borderRadius: 999,
                                       background: b.bg, color: b.fg, fontWeight: weight.semibold }}>
                          {b.txt}
                        </span>
                        <span style={{ fontSize: "0.7rem", color: t.n3 }}>
                          {row.submissions} submission{row.submissions === 1 ? "" : "s"} · {row.annotators} annotator{row.annotators === 1 ? "" : "s"}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        {/* the submissions on the selected task */}
        <section style={{ overflowY: "auto", padding: 20 }}>
          {!selected ? (
            <p style={{ color: t.n3, fontSize: "0.82rem" }}>Pick a task to review its submissions.</p>
          ) : subs === null ? (
            <p style={{ color: t.n3, fontSize: "0.82rem" }}>Loading…</p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 820 }}>
              {error && (
                <p role="alert" style={{ fontSize: "0.78rem", color: t.redDark, background: tint(t.red, 12),
                                         padding: "9px 12px", borderRadius: t.radiusLg, margin: 0 }}>
                  {error}
                </p>
              )}
              {subs.map((s) => (
                <article key={s.submissionId}
                         style={{ background: t.n9, border: `1px solid ${s.accepted ? t.green : t.n7}`,
                                  borderRadius: t.radiusLg, padding: 14 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <span style={{ fontWeight: weight.semibold, fontSize: "0.84rem" }}>{s.annotator}</span>
                    <span style={{ fontSize: "0.72rem", padding: "2px 8px", borderRadius: 999,
                                   background: s.reward === 1 ? tint(t.green, 14) : tint(t.yellow, 16),
                                   color: s.reward === 1 ? t.greenDark : t.n1, fontWeight: weight.semibold }}>
                      reward {s.reward}
                    </span>
                    {s.accepted && (
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: "0.72rem",
                                     color: t.greenDark, fontWeight: weight.semibold }}>
                        <Icon name="check" size={13} stroke={2.4} color={t.greenDark} /> golden
                      </span>
                    )}
                    <span style={{ flex: 1 }} />
                    <span style={{ fontSize: "0.7rem", color: t.n3 }}>{new Date(s.at).toLocaleString()}</span>
                  </div>

                  <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
                    <Button variant="primary" disabled={busy || s.accepted}
                            onClick={() => void accept(s.sessionId)}>
                      Accept as golden
                    </Button>
                    <Button variant="soft" disabled={busy}
                            onClick={() => { setReturning(returning === s.submissionId ? null : s.submissionId); setNote(""); setError(null); }}>
                      Send back
                    </Button>
                    <Button variant="soft" onClick={() => void downloadSampleBundle(s.sessionId)}>
                      ⬇ bundle
                    </Button>
                  </div>

                  {returning === s.submissionId && (
                    <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
                      <label style={{ fontSize: "0.74rem", color: t.n2, fontWeight: weight.semibold }}>
                        What should they fix? The annotator sees this.
                      </label>
                      <textarea
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                        rows={3}
                        placeholder="e.g. the cart was left with an extra item — remove it before checking out"
                        style={{ fontFamily: "inherit", fontSize: "0.8rem", padding: 9, borderRadius: t.radiusLg,
                                 border: `1px solid ${t.n7}`, resize: "vertical" }}
                      />
                      <div style={{ display: "flex", gap: 8 }}>
                        <Button variant="primary" disabled={busy || !note.trim()}
                                onClick={() => void sendBack(s.submissionId)}>
                          Send it back
                        </Button>
                        <Button variant="soft" disabled={busy} onClick={() => setReturning(null)}>
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </article>
              ))}
              {subs.length === 0 && (
                <p style={{ color: t.n3, fontSize: "0.82rem" }}>No submissions on this task.</p>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
