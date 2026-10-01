import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import pwaPlugin from "./pwa-plugin.js";

// The installed app (/m) shares icons, the api modules and React with the lazy pages. Left to
// Rollup, every shared icon becomes its own 300-byte chunk (two dozen requests and their per-file
// overhead on the /m budget). So: the libraries /m loads statically and the icons its code
// imports go into one "vendor" chunk, and the app code it shares with the full app (chat, files,
// the api modules, i18n) into one "shared" chunk. Every page stays lazy.
const M_ENTRY = /\/src\/mobile\/main\.tsx$/;
const ICON = /\/node_modules\/lucide-react\/dist\/esm\/icons\/([\w-]+)\.m?js$/;
const iconsOf = new Map<string, string[]>(); // module -> the lucide icons it imports (kebab-case)
const kebab = (name: string) =>
  name.replace(/Icon$/, "").replace(/([a-z0-9])([A-Z])/g, "$1-$2").replace(/([a-zA-Z])(\d)/g, "$1-$2").toLowerCase();

function collectIcons() {
  return {
    name: "pos-collect-icons",
    moduleParsed(info: { id: string; code: string | null }) {
      if (!info.code || info.id.includes("/node_modules/")) return;
      const names = [...info.code.matchAll(/import\s*{([^}]*)}\s*from\s*["']lucide-react["']/g)].flatMap((m) =>
        m[1].split(",").map((x) => x.trim().split(/\s+as\s+/)[0]).filter((x) => x && x !== "type"),
      );
      if (names.length) iconsOf.set(info.id, names.map(kebab));
    },
  };
}

type Info = { importers: readonly string[] } | null;
const reached = new Map<string, boolean>();
/** Is `id` part of `entry`'s static import graph (dynamic imports do not count)? */
function reachedFrom(id: string, entry: RegExp, info: (id: string) => Info): boolean {
  if (!reached.has(id)) {
    const seen = new Set<string>();
    const walk = (x: string): boolean => {
      if (entry.test(x) || reached.get(x)) return true;
      if (seen.has(x)) return false;
      seen.add(x);
      return (info(x)?.importers ?? []).some(walk);
    };
    reached.set(id, walk(id));
  }
  return reached.get(id)!;
}

let mIcons: Set<string> | null = null;
function vendorChunk(id: string, info: (id: string) => Info): string | undefined {
  if (!id.includes("/node_modules/")) {
    const sharedApp = /\/src\//.test(id) && !/\/src\/mobile\//.test(id);
    return sharedApp && reachedFrom(id, M_ENTRY, info) ? "shared" : undefined;
  }
  const icon = ICON.exec(id);
  if (icon) {
    mIcons ??= new Set([...iconsOf].filter(([mod]) => reachedFrom(mod, M_ENTRY, info)).flatMap(([, n]) => n));
    return mIcons.has(icon[1]) ? "vendor" : undefined;
  }
  return reachedFrom(id, M_ENTRY, info) ? "vendor" : undefined;
}

declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  // pwaPlugin: the installed app's service worker and the /m entry in dev (docs/MOBILE.md).
  plugins: [react(), tailwindcss(), pwaPlugin(), collectIcons()],
  build: {
    manifest: true, // the chunk graph, for scripts/check-pwa.mjs (the /m size budget)
    rollupOptions: {
      // The full app, and the installed app at /m (its own small bundle: chat, "Čeká na tebe", tasks).
      input: { main: "index.html", m: "m.html" },
      output: {
        manualChunks: (id, { getModuleInfo }) => vendorChunk(id, getModuleInfo),
      },
    },
  },
  server: {
    port: 5173,
    // In dev the API runs separately (uvicorn on :8000); in Docker nginx does this.
    proxy: { "/api": process.env.POS_API ?? "http://localhost:8000" },
  },
});
