import { useState } from "react";
import { AuthProvider, useAuth } from "./features/auth/AuthContext";
import { LoginScreen } from "./features/auth/LoginScreen";
import { MyTasks } from "./features/my-tasks/MyTasks";
import { TaskReview } from "./features/task-review/TaskReview";

function Gate() {
  const { annotator, loading } = useAuth();
  // Which task the annotator is working, if any. Signing in lands on the board
  // (My tasks); picking a task opens the live-browser annotation screen for it,
  // and leaving returns to the board — so the board, not a single task, is home.
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);

  if (loading) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", color: "#8b94a3", fontFamily: "ui-sans-serif, system-ui, sans-serif", fontSize: 14 }}>
        Loading…
      </div>
    );
  }
  if (!annotator) return <LoginScreen />;
  if (openTaskId) {
    return (
      <TaskReview
        key={openTaskId}
        initialTaskId={openTaskId}
        onExitToTasks={() => setOpenTaskId(null)}
      />
    );
  }
  return <MyTasks onOpenTask={setOpenTaskId} />;
}

export function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
