import { useState, type ReactNode } from "react";
import { Button, Icon, Tag, t, weight } from "../../../ds";
import type { Metric, Task } from "../../../lib/types";

function Label({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
      <span style={{ fontSize: "0.6875rem", fontWeight: weight.bold, letterSpacing: "0.07em", color: t.n3, textTransform: "uppercase" }}>
        {children}
      </span>
      {action}
    </div>
  );
}

function metricColor(tone: Metric["tone"]) {
  if (tone === "error") return t.red;
  if (tone === "success") return t.greenDark;
  return t.n0;
}

export function RightPanel({ task, summary, onSavePrompt, rerunsOnSave }: { task: Task; summary: Metric[]; onSavePrompt?: (text: string) => void; rerunsOnSave?: boolean }) {
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [promptText, setPromptText] = useState(task.prompt);
  const startEdit = () => { setPromptText(task.prompt); setEditingPrompt(true); };
  const savePrompt = () => {
    const next = promptText.trim() || task.prompt;
    // A gym prompt edit re-drives the whole run — confirm the (slow, destructive-
    // to-the-current-review) action so it isn't triggered by accident.
    if (rerunsOnSave && next !== task.prompt && !window.confirm("Re-run the whole task under this new prompt? The current review will be replaced by a fresh run.")) return;
    onSavePrompt?.(next);
    setEditingPrompt(false);
  };

  // Trailing agent id in mono (design §4.1: "nav-agent-v4" in --font-mono).
  const metaParts = task.meta.split(" · ");
  const metaAgent = metaParts.length > 1 ? metaParts[metaParts.length - 1] : "";
  const metaRest = metaParts.length > 1 ? metaParts.slice(0, -1).join(" · ") + " · " : task.meta;

  return (
    <aside style={{ width: 360, height: "100%", flexShrink: 0, background: t.n9, border: `1px solid ${t.n7}`, borderRadius: t.radiusXl, boxShadow: t.shadowMd, overflow: "hidden", display: "flex", flexDirection: "column" }}>
      {/* fixed header (§4.1) */}
      <div style={{ padding: "16px 18px 14px", borderBottom: `1px solid ${t.n7}`, flexShrink: 0 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span style={{ fontFamily: t.fontMono, fontSize: "0.6875rem", padding: "3px 8px", background: t.deltaTagId, border: `1px solid ${t.n6}`, borderRadius: 5, color: t.n1 }}>{task.id}</span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: "0.75rem", fontWeight: weight.semibold, color: t.redDark }}>
            <span style={{ width: 7, height: 7, borderRadius: t.radiusFull, background: t.red }} />
            {task.priority}
          </span>
        </div>
        <h1 style={{ margin: "12px 0 0", fontSize: "1.0625rem", lineHeight: 1.3, fontWeight: weight.bold, color: t.n0, letterSpacing: "-0.3px" }}>
          {task.title}
        </h1>
        <div style={{ marginTop: 7, fontSize: "0.75rem", color: t.n3 }}>{metaRest}<span style={{ fontFamily: t.fontMono }}>{metaAgent}</span></div>
      </div>

      {/* scroll body (§4.2) */}
      <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "16px 18px", display: "flex", flexDirection: "column", gap: 18 }}>
        <div>
          <Label action={!editingPrompt && (
            <span onClick={startEdit} style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: "0.75rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer" }}><Icon name="pencil" size={13} /> Edit</span>
          )}>Task prompt</Label>
          {editingPrompt ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <textarea value={promptText} onChange={(e) => setPromptText(e.target.value)} autoFocus
                style={{ width: "100%", boxSizing: "border-box", minHeight: 128, resize: "vertical", padding: "10px 12px", border: `1px solid ${t.primary6}`, borderRadius: t.radiusLg, fontFamily: t.fontPrimary, fontSize: "0.8125rem", lineHeight: 1.55, color: t.n0, outline: "none" }} />
              {rerunsOnSave && (
                <span style={{ fontSize: "0.72rem", color: t.n3, lineHeight: 1.5 }}>Saving re-drives the whole task under the new prompt (a live agent run), then a fresh review.</span>
              )}
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
                <Button variant="secondary" onClick={() => setEditingPrompt(false)}>Cancel</Button>
                <Button onClick={savePrompt}>{rerunsOnSave ? "Save & re-run" : "Save prompt"}</Button>
              </div>
            </div>
          ) : (
            <p style={{ margin: 0, fontSize: "0.84rem", lineHeight: 1.55, color: t.n1 }}>{task.prompt}</p>
          )}
        </div>

        <div>
          <Label>Start state</Label>
          <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "0.78rem", color: t.n2 }}>
            <span style={{ width: 6, height: 6, borderRadius: t.radiusFull, background: t.n4 }} />
            {task.startState.summary}
          </div>
          <div style={{ marginTop: 6, padding: "7px 10px", background: t.n85, border: `1px solid ${t.n7}`, borderRadius: t.radiusMd, fontFamily: t.fontMono, fontSize: "0.75rem", color: t.n1, overflowX: "auto" }}>
            {task.startState.url}
          </div>
        </div>

        {/* Each of these renders ONLY when it has something to say. They used to
            render their heading unconditionally, so a gym task — which carries no
            constraints and, until its first run, no summary — showed two bare
            headings and an empty grey box. A heading with nothing under it reads
            as "this failed to load", which is worse than not being there. */}
        {task.constraints.length > 0 && (
          <div>
            <Label>Constraints</Label>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {task.constraints.map((c) => (<Tag key={c}>{c}</Tag>))}
            </div>
          </div>
        )}

        {task.allowedSites.length > 0 && (
          <div>
            <Label>Allowed sites</Label>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {/* `host` can be missing when the payload came from the gym task
                  picker, which names apps rather than hosts. Falling back to the
                  app keeps a labelled chip instead of a bare coloured dot with no
                  text beside it. */}
              {task.allowedSites.map((sIt, i) => (
                <Tag key={sIt.host || sIt.app || i} dot={sIt.color}>{sIt.host || sIt.app || "—"}</Tag>
              ))}
            </div>
          </div>
        )}

        {summary.length > 0 && (
          <div>
            <Label>Run summary</Label>
            <div style={{ background: t.n85, border: `1px solid ${t.n7}`, borderRadius: 10, padding: 14, display: "grid", gridTemplateColumns: "1fr 1fr", rowGap: 12, columnGap: 16 }}>
              {summary.map((m) => (
                <div key={m.label}>
                  <div style={{ fontFamily: t.fontMono, fontSize: "1.0625rem", fontWeight: weight.bold, lineHeight: 1, color: metricColor(m.tone) }}>{m.value}</div>
                  <div style={{ marginTop: 4, fontSize: "0.719rem", color: t.n3 }}>{m.label}</div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}
