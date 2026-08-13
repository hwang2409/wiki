import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "katex/dist/katex.min.css";
import "react-diff-view/style/index.css";
import App from "./App";
import { installExternalLinkInterceptors } from "./external-links";
import "./styles.css";
import "./themes.css";
import { applyStoredTheme } from "./themes";
import { applyStoredFonts } from "./settings";
import { applyStoredLowercase } from "./lowercase-mode";
import { setThumbnailCacheNamespace } from "./thumbnail-cache";
import { hydrateFromServer, installUiStateWriteBack } from "./ui-state-sync";

async function applyVaultNamespace() {
  // Namespace the artifact thumbnail cache by the backend's vault
  // identity so previews from different vaults sharing this origin
  // cannot collide. Failure to fetch is non-fatal — the cache stays
  // on its "default" namespace and mtime invalidation still runs.
  try {
    const response = await fetch("/api/vault/identity", { cache: "no-store" });
    if (!response.ok) return;
    const body = await response.json();
    if (typeof body?.identity === "string" && body.identity) {
      setThumbnailCacheNamespace(body.identity);
    }
  } catch {
    // Non-fatal — leave the default namespace in place.
  }
}

async function bootstrap() {
  await Promise.all([hydrateFromServer(), applyVaultNamespace()]);
  installUiStateWriteBack();
  installExternalLinkInterceptors();
  applyStoredTheme();
  applyStoredFonts();
  applyStoredLowercase();

  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>
  );
}

void bootstrap();
