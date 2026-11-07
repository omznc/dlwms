import { defineConfig } from "vite";
import { cloudflare } from "@cloudflare/vite-plugin";

// cf deploy builds the Worker with this plugin. The Worker has no static assets: GitHub Pages serves the site.
export default defineConfig({ plugins: [cloudflare()] });
