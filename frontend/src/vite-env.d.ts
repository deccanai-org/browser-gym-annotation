/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Where the live pane reaches the live-browser service. Set at BUILD time for a
   *  hosted deploy (the live-browser's public URL); empty falls back to
   *  http://localhost:8877 for local dev. See lib/liveBrowser.ts. */
  readonly VITE_LIVE_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
