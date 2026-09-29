// The dictionary areas the first screens need, before anything reads a string (i18n/core.ts).
import "./dict";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "../index.css";
import MobileApp from "./MobileApp";
import { registerServiceWorker } from "./pwa";

// The installed app at /m (docs/MOBILE.md): its own small entry, the service worker, push.
registerServiceWorker();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <MobileApp />
    </BrowserRouter>
  </StrictMode>,
);

// The rest of the dictionary (approval names and the like) when the phone is idle.
const idle = (window as Window & { requestIdleCallback?: (f: () => void) => void }).requestIdleCallback ?? ((f: () => void) => setTimeout(f, 1500));
// Lists drawn before it arrived redraw (the needs list re-reads, e.g. approval names in words).
idle(() => void import("../i18n").then(() => window.dispatchEvent(new Event("pos:needs-me"))));
