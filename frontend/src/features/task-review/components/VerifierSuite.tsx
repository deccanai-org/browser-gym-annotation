import { useState } from "react";
import { Button, Icon, Meter, t, weight, VERIFIER_LEVEL } from "../../../ds";
import type { VerifierLevel } from "../../../ds";
import type { ReviewState, Verifier } from "../../../lib/types";
import {
  allVerifiers,
  canSubmit,
  failingCount,
  levelScore,
  levelVerifiers,
  reward,
  verifierState,
} from "../../../lib/reviewMachine";
import { BenchmarkDock } from "./BenchmarkDock";

const LEVELS = Object.keys(VERIFIER_LEVEL) as VerifierLevel[];

function LevelChip({ level }: { level: VerifierLevel }) {
  const L = VERIFIER_LEVEL[level];
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 7, padding: "5px 11px", borderRadius: t.radiusPill, background: t.n85, border: `1px solid ${t.n7}`, fontSize: "0.75rem", fontWeight: weight.semibold, color: t.n2 }}>
      <span style={{ width: 8, height: 8, borderRadius: 2, background: L.color }} />
      {L.label}
    </span>
  );
}

function EmptyState({ unlocked, onGenerate }: { unlocked: boolean; onGenerate: () => void }) {
  return (
    <div style={{ padding: "48px 24px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center" }}>
      <span style={{ width: 52, height: 52, borderRadius: 14, background: t.primary0, color: t.primary6, display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
        <Icon name="checkSquare" size={26} color={t.primary6} />
      </span>
      <div style={{ marginTop: 16, fontSize: "1rem", fontWeight: weight.bold, color: t.n0 }}>
        {unlocked ? "Steps approved — ready to build verifiers" : "Approve the steps first"}
      </div>
      <div style={{ marginTop: 6, fontSize: "0.8125rem", color: t.n2, maxWidth: 440, lineHeight: 1.55 }}>
        {unlocked
          ? "Generate a verifier suite. Each level gets multiple typed checks you can edit and extend before running the benchmark."
          : "Review and correct the agent run above, then approve all steps. The verifier suite unlocks once the trace is approved."}
      </div>
      <div style={{ margin: "22px 0", display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "center" }}>
        {LEVELS.map((lv) => (
          <LevelChip key={lv} level={lv} />
        ))}
      </div>
      <Button disabled={!unlocked} onClick={onGenerate} style={{ minHeight: 48, fontSize: "1rem", padding: "0 1.75rem" }}>Generate verifier suite</Button>
    </div>
  );
}

function VerifierRow({ v, state, benchmarkRun, first, onOverride, onEdit }: { v: Verifier; state: ReviewState; benchmarkRun: boolean; first: boolean; onOverride: (id: string) => void; onEdit: (id: string, assertion: string, code: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [assertion, setAssertion] = useState(v.assertion);
  const [code, setCode] = useState(v.code);

  const startEdit = () => { setAssertion(v.assertion); setCode(v.code); setEditing(true); };
  const inputStyle = { width: "100%", boxSizing: "border-box" as const, padding: "8px 10px", borderRadius: 7, border: `1px solid ${t.primary6}`, fontFamily: t.fontPrimary, fontSize: "0.78rem", fontWeight: weight.semibold, color: t.n0, outline: "none" };

  if (editing) {
    return (
      <div style={{ padding: "12px 14px", borderTop: first ? "none" : `1px solid ${t.n7}`, display: "flex", flexDirection: "column", gap: 10 }}>
        <input value={assertion} onChange={(e) => setAssertion(e.target.value)} style={inputStyle} />
        <textarea value={code} onChange={(e) => setCode(e.target.value)} rows={2} style={{ ...inputStyle, fontWeight: weight.regular, fontFamily: t.fontMono, background: t.n85, border: `1px solid ${t.primary6}`, resize: "vertical" }} />
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <Button variant="secondary" onClick={() => setEditing(false)}>Cancel</Button>
          <Button onClick={() => { onEdit(v.id, assertion || v.assertion, code); setEditing(false); }}>Save</Button>
        </div>
      </div>
    );
  }

  const vs = verifierState(state, v);
  const meterState = benchmarkRun ? vs : "pending";
  const overridden = benchmarkRun && state.overrides[v.id];
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 16, padding: "12px 14px", borderTop: first ? "none" : `1px solid ${t.n7}` }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: "0.78rem", fontWeight: weight.semibold, color: t.n0, lineHeight: 1.4 }}>{v.assertion}</div>
        <div style={{ marginTop: 4, fontFamily: t.fontMono, fontSize: "0.6875rem", color: t.n2, lineHeight: 1.45, wordBreak: "break-word" }}>{v.code}</div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
        {overridden ? (
          <span onClick={() => onOverride(v.id)} title="Remove override" style={{ cursor: "pointer", fontSize: "0.6875rem", fontWeight: weight.bold, color: t.purple, background: `color-mix(in srgb, ${t.purple} 12%, transparent)`, padding: "4px 9px", borderRadius: t.radiusMd, whiteSpace: "nowrap" }}>1 override</span>
        ) : (
          <>
            <Meter state={meterState} />
            {benchmarkRun && vs === "fail" && (
              <span onClick={() => onOverride(v.id)} style={{ cursor: "pointer", padding: "3px 8px", borderRadius: 6, border: `1px solid ${t.n6}`, fontSize: "0.656rem", fontWeight: weight.bold, color: t.n2, whiteSpace: "nowrap" }}>Override</span>
            )}
          </>
        )}
        <span onClick={startEdit} title="Edit verifier" style={{ cursor: "pointer", width: 24, height: 24, borderRadius: 6, display: "inline-flex", alignItems: "center", justifyContent: "center", color: t.n3 }}>
          <Icon name="pencil" size={13} color={t.n3} />
        </span>
      </div>
    </div>
  );
}

function AddEditor({ level, onCancel, onSave }: { level: VerifierLevel; onCancel: () => void; onSave: (assertion: string, code: string) => void }) {
  const [assertion, setAssertion] = useState("");
  const [code, setCode] = useState("assert /* define check */");
  const inputStyle = { width: "100%", padding: "9px 12px", borderRadius: t.radiusLg, border: `1px solid ${t.primary6}`, fontFamily: t.fontPrimary, fontSize: "0.875rem", color: t.n0, outline: "none", boxSizing: "border-box" as const };
  return (
    <div style={{ padding: "14px 18px", borderTop: `1px solid ${t.n7}`, display: "flex", flexDirection: "column", gap: 10 }}>
      <input value={assertion} placeholder="New verifier assertion" onChange={(e) => setAssertion(e.target.value)} style={inputStyle} />
      <textarea value={code} onChange={(e) => setCode(e.target.value)} rows={3} style={{ ...inputStyle, fontFamily: t.fontMono, background: t.n85, border: `1px solid ${t.n7}`, resize: "vertical" }} />
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <Button variant="secondary" onClick={onCancel}>Cancel</Button>
        <Button onClick={() => onSave(assertion || "New verifier assertion", code)}>Save</Button>
      </div>
      <div style={{ fontSize: "0.72rem", color: t.n3 }}>Level: {VERIFIER_LEVEL[level].label}. An empty or placeholder check scores 0 — never 1.</div>
    </div>
  );
}

export function VerifierSuite({
  state,
  onGenerate,
  onSetLevel,
  onAddVerifier,
  onEditVerifier,
  onOverride,
  onRun,
  onSubmit,
  submitNote,
}: {
  state: ReviewState;
  onGenerate: () => void;
  onSetLevel: (l: VerifierLevel) => void;
  onAddVerifier: (assertion: string, code: string) => void;
  onEditVerifier: (id: string, assertion: string, code: string) => void;
  onOverride: (id: string) => void;
  onRun: () => void;
  /** Absent on an attempt with a version graph — that attempt ships through
   *  finalize instead, and `submitNote` says so where the button used to be. */
  onSubmit?: () => void;
  submitNote?: string;
}) {
  const [adding, setAdding] = useState(false);

  const cardShell = { background: t.n9, border: `1px solid ${t.n7}`, borderRadius: t.radiusXl, boxShadow: t.shadowMd, overflow: "hidden" } as const;

  if (!state.stepsApproved) return <div style={cardShell}><EmptyState unlocked={false} onGenerate={onGenerate} /></div>;
  if (!state.verifiersGenerated) return <div style={cardShell}><EmptyState unlocked onGenerate={onGenerate} /></div>;

  const L = VERIFIER_LEVEL[state.activeLevel];
  const group = levelVerifiers(state, state.activeLevel);
  const scoreColorFor = (pass: number, total: number) => (!state.benchmarkRun ? t.n3 : pass === total ? t.greenDark : t.red);
  const groupSc = levelScore(state, state.activeLevel);

  return (
    <div style={cardShell}>
      {/* level tab bar */}
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", padding: "0 20px", borderBottom: `1px solid ${t.n7}` }}>
        {LEVELS.map((lv) => {
          const active = lv === state.activeLevel;
          const sc = levelScore(state, lv);
          const V = VERIFIER_LEVEL[lv];
          return (
            <div key={lv} onClick={() => onSetLevel(lv)} style={{ display: "flex", alignItems: "center", gap: 7, padding: "9px 14px", cursor: "pointer", borderBottom: active ? `2px solid ${t.primary6}` : "2px solid transparent", marginBottom: -1 }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: V.color }} />
              <span style={{ fontSize: "0.8125rem", fontWeight: weight.semibold, color: active ? t.n0 : t.n3 }}>{V.label}</span>
              <span style={{ fontFamily: t.fontMono, fontSize: "0.6875rem", fontWeight: weight.bold, color: scoreColorFor(sc.pass, sc.total) }}>{state.benchmarkRun ? `${sc.pass} / ${sc.total}` : `${sc.total} checks`}</span>
            </div>
          );
        })}
      </div>

      {/* active group */}
      <div style={{ padding: "16px 20px" }}>
        <div style={{ maxWidth: 780, border: `1px solid ${t.n7}`, borderRadius: t.radiusXl, overflow: "hidden" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, padding: "11px 14px", background: t.n85, borderBottom: `1px solid ${t.n7}` }}>
            <span style={{ width: 9, height: 9, borderRadius: 3, background: L.color }} />
            <span style={{ fontSize: "0.8125rem", fontWeight: weight.bold, color: t.n0 }}>{L.label}</span>
            <span style={{ fontSize: "0.625rem", fontWeight: weight.bold, textTransform: "uppercase", letterSpacing: "0.04em", color: L.color, background: `color-mix(in srgb, ${L.color} 12%, transparent)`, padding: "2px 7px", borderRadius: 5 }}>{L.chip}</span>
            <span style={{ flex: 1 }} />
            <span style={{ fontFamily: t.fontMono, fontSize: "0.719rem", fontWeight: weight.bold, color: scoreColorFor(groupSc.pass, group.length) }}>{state.benchmarkRun ? `${groupSc.pass} / ${group.length}` : `${group.length} checks`}</span>
          </div>

          {group.map((v, i) => (
            <VerifierRow key={v.id} v={v} state={state} benchmarkRun={state.benchmarkRun} first={i === 0} onOverride={onOverride} onEdit={onEditVerifier} />
          ))}

          {adding ? (
            <AddEditor level={state.activeLevel} onCancel={() => setAdding(false)} onSave={(a, c) => { onAddVerifier(a, c); setAdding(false); }} />
          ) : (
            <div onClick={() => setAdding(true)} style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", borderTop: `1px solid ${t.n7}`, color: t.primary6, fontSize: "0.78rem", fontWeight: weight.semibold, cursor: "pointer" }}>
              <Icon name="plus" size={16} color={t.primary6} /> Add a verifier to {L.label}
            </div>
          )}
        </div>
      </div>

      <BenchmarkDock
        reward={reward(state)}
        benchmarkRun={state.benchmarkRun}
        failing={failingCount(state)}
        total={allVerifiers(state).length}
        canSubmit={canSubmit(state)}
        submitted={state.submitted}
        submittedKind={state.serverSubmission?.kind ?? null}
        submitError={state.submitError}
        onRun={onRun}
        onSubmit={onSubmit}
        submitNote={submitNote}
      />
    </div>
  );
}
