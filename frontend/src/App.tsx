import { useState } from "react";
import { t } from "./ds";
import { AuthProvider, useAuth } from "./features/auth/AuthContext";
import { LoginScreen } from "./features/auth/LoginScreen";
import { MyTasks } from "./features/my-tasks/MyTasks";
import { QaScreen } from "./features/qa/QaScreen";
import { TaskReview } from "./features/task-review/TaskReview";

const REVIEWER_ROLES = new Set(["reviewer", "admin"]);

function Gate() {
  const { annotator, loading } = useAuth();
  // Which task the annotator is working, if any. Signing in lands on the board
  // (My tasks); picking a task opens the live-browser annotation screen for it,
  // and leaving returns to the board — so the board, not a single task, is home.
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  // QA is its own screen. As a modal inside a task it could only be reached by
  // opening somebody's annotation, which boots a real Chromium and leases a gym
  // — a heavy price for reading a list of submissions.
  const [qaOpen, setQaOpen] = useState(false);

  if (loading) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", color: t.n3, fontFamily: t.fontPrimary, fontSize: 14 }}>
        Loading…
      </div>
    );
  }
  if (!annotator) return <LoginScreen />;
  // Belt and braces with the server's own gate: the QA endpoints refuse a
  // non-reviewer, and this stops the screen appearing at all for one.
  const isReviewer = REVIEWER_ROLES.has(annotator.role);
  if (qaOpen && isReviewer) return <QaScreen onExit={() => setQaOpen(false)} />;
  if (openTaskId) {
    return (
      <TaskReview
        key={openTaskId}
        initialTaskId={openTaskId}
        onExitToTasks={() => setOpenTaskId(null)}
      />
    );
  }
  return (
    <MyTasks
      onOpenTask={setOpenTaskId}
      onOpenQa={isReviewer ? () => setQaOpen(true) : undefined}
    />
  );
}

export function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
