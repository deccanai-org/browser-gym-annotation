import { GoogleLogin } from "@react-oauth/google";
import type { CredentialResponse } from "@react-oauth/google";

/** Google Sign-In button (Google Identity Services), mirroring the reference
 *  auth app. On success it hands the parent the ID token (`credential`). */
export function GoogleAuthButton({
  onCredential,
  onError,
  text = "continue_with",
}: {
  onCredential: (credential: string) => void;
  onError?: () => void;
  text?: "signin_with" | "signup_with" | "continue_with" | "signin";
}) {
  return (
    <GoogleLogin
      text={text}
      shape="pill"
      theme="outline"
      size="large"
      useOneTap={false}
      auto_select={false}
      onSuccess={(res: CredentialResponse) => {
        if (res.credential) onCredential(res.credential);
        else onError?.();
      }}
      onError={() => onError?.()}
    />
  );
}
