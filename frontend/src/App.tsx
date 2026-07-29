import { GoogleOAuthProvider } from "@react-oauth/google";
import { AuthProvider, useAuth } from "./features/auth/AuthContext";
import { GOOGLE_CLIENT_ID } from "./features/auth/config";
import { LoginScreen } from "./features/auth/LoginScreen";
import { TaskReview } from "./features/task-review/TaskReview";

function Gate() {
  const { annotator, loading } = useAuth();
  if (loading) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", color: "#8b94a3", fontFamily: "ui-sans-serif, system-ui, sans-serif", fontSize: 14 }}>
        Loading…
      </div>
    );
  }
  if (!annotator) return <LoginScreen />;
  return <TaskReview />;
}

export function App() {
  return (
    <GoogleOAuthProvider clientId={GOOGLE_CLIENT_ID}>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </GoogleOAuthProvider>
  );
}
