import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend serves this page itself (routes/shopify_admin.py):
//   GET /shopify/app                     -> dist/index.html (API key filled in)
//   GET /shopify/app-assets/<file>       -> dist/app-assets/<file>
// so asset URLs must be relative (base "./") and the assets folder must be
// "app-assets" — it has to match _ASSETS_DIR in routes/shopify_admin.py.
//
// Build output goes to ./dist. Copy that folder to the backend's
// shopify-admin/dist (or point SHOPIFY_ADMIN_DIST at it). Only the built
// files go into the backend repo, not this source.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    assetsDir: "app-assets",
    emptyOutDir: true,
  },
});
