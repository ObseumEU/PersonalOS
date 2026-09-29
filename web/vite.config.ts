import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import pwaPlugin from "./pwa-plugin.js";

declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  // pwaPlugin: the installed app's service worker and the /m entry in dev (docs/MOBILE.md).
  plugins: [react(), tailwindcss(), pwaPlugin()],
  build: {
    manifest: true, // the chunk graph, for scripts/check-pwa.mjs (the /m size budget)
    rollupOptions: {
      // The full app, and the installed app at /m (its own small bundle: chat, "Čeká na tebe", tasks).
      input: { main: "index.html", m: "m.html" },
    },
  },
  server: {
    port: 5173,
    // In dev the API runs separately (uvicorn on :8000); in Docker nginx does this.
    proxy: { "/api": process.env.POS_API ?? "http://localhost:8000" },
  },
});
