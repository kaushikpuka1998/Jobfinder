import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API calls to Flask; `npm run build` outputs to
// ../frontend/dist, which app/main.py serves directly when it exists.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:5001",
      "/download": "http://127.0.0.1:5001",
      "/apply": "http://127.0.0.1:5001",
    },
  },
  build: {
    outDir: "dist",
  },
});
