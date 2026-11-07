import { defineConfig } from "cf/config";

export default defineConfig({
	worker: {
		name: "dlwms",
		compatibilityDate: "2026-10-01",
		entrypoint: "src/index.js",
		domains: ["dlwms-gh.omarzunic.com"],
		workersDev: false,
		previewUrls: false,
	},
});
