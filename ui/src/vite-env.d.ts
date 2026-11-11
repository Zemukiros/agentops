/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_JAEGER_URL?: string;
  /** "1" builds the static recorded demo (ADR-053): no backend, bundled recording. */
  readonly VITE_DEMO?: string;
}
