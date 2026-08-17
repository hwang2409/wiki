import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/inter";
import "@fontsource/fira-code/400.css";
import "katex/dist/katex.min.css";
import "react-diff-view/style/index.css";
import App from "./App";
import { installExternalLinkInterceptors } from "./external-links";
import { PrimitivesDemo } from "./primitives-demo";
import "./styles.css";
import "./themes.css";
import { applyStoredTheme } from "./themes";
import { applyStoredFonts } from "./settings";
import { applyStoredLowercase } from "./lowercase-mode";
import { setThumbnailCacheNamespace } from "./thumbnail-cache";
import { hydrateFromServer, installUiStateWriteBack } from "./ui-state-sync";

// WIKI-297: an isolated visual inventory of the bb-parity primitives.
// Intercept before boot so the demo renders without pulling the full
// App shell (which touches routing/panes/network) and so wave-2/3
// consumers can validate primitives in isolation.
const PRIMITIVES_DEMO_HASH = "#/primitives-demo";

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
  const isPrimitivesDemo = window.location.hash === PRIMITIVES_DEMO_HASH;

  if (isPrimitivesDemo) {
    applyStoredTheme();
    applyStoredFonts();
    applyStoredLowercase();
    createRoot(document.getElementById("root")!).render(
      <StrictMode>
        <PrimitivesDemo />
      </StrictMode>
    );
    return;
  }

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
