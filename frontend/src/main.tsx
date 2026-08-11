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
import { hydrateFromServer, installUiStateWriteBack } from "./ui-state-sync";

async function bootstrap() {
  await hydrateFromServer();
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
