import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // In dev the API runs separately (uvicorn on :8000); in Docker nginx does this.
    proxy: { "/api": process.env.POS_API ?? "http://localhost:8000" },
  },
});
