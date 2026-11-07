// Serve the site from GitHub Pages and the course files from GitHub releases, through the edge cache.
// The site is public: there is no auth.
const PAGES = "https://omznc.github.io/dlwms";
const RELEASES = "https://github.com/omznc/dlwms/releases/download";
// Pagefind file names have a content hash, except these entry files.
const ENTRY = /^\/pagefind\/pagefind[^/]*\.(js|json|css)$/;
const YEAR = 31536000;
// Part of the cache key. Increase it after a change to the templates or /static/, so the edge drops the old item pages.
const VERSION = 1;

function cacheControl(path) {
	if (path.startsWith("/files/") || path.startsWith("/img/")) return `public, max-age=${YEAR}, immutable`;
	if (path.startsWith("/pagefind/") && !ENTRY.test(path)) return `public, max-age=${YEAR}, immutable`;
	// An item never changes after it is posted. The browser checks again after a day, for a template change.
	if (path.startsWith("/item/")) return `public, max-age=86400, s-maxage=${YEAR}`;
	// The home page, /static/ and the Pagefind entry files change with each deploy.
	return "public, max-age=300";
}

async function courseFile(path) {
	// build.py links /files/<release tag>/<asset name>/<file name>.
	const [tag, asset, name, extra] = path.slice("/files/".length).split("/");
	if (!tag || !asset || !name || extra !== undefined) return null;
	let filename;
	try {
		filename = decodeURIComponent(name);
	} catch {
		return null;
	}
	const response = await fetch(`${RELEASES}/${encodeURIComponent(tag)}/${encodeURIComponent(asset)}`);
	if (!response.ok) return null;
	// Always download: an uploaded HTML or SVG file must not run on this origin.
	return new Response(response.body, {
		headers: {
			"Content-Type": "application/octet-stream",
			"Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
			"X-Content-Type-Options": "nosniff",
		},
	});
}

async function page(url) {
	const response = await fetch(PAGES + url.pathname + url.search, { redirect: "manual" });
	// GitHub Pages adds the trailing slash with a redirect to the project path.
	const location = response.headers.get("Location");
	if (location?.startsWith(PAGES + "/")) {
		return new Response(null, { status: response.status, headers: { Location: location.slice(PAGES.length) } });
	}
	return response;
}

export default {
	async fetch(request, env, ctx) {
		if (request.method !== "GET" && request.method !== "HEAD") {
			return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
		}
		const url = new URL(request.url);
		const keyUrl = new URL(url);
		keyUrl.searchParams.set("__v", VERSION);
		const key = new Request(keyUrl.toString());
		const cached = await caches.default.match(key);
		if (cached) return cached;
		const origin = url.pathname.startsWith("/files/") ? await courseFile(url.pathname) : await page(url);
		if (!origin) return new Response("Not found", { status: 404 });
		// Cache only a 200: a 404 for a new Pagefind file must not stay in the cache.
		if (origin.status !== 200) return origin;
		const response = new Response(origin.body, origin);
		response.headers.delete("Expires");
		response.headers.set("Cache-Control", cacheControl(url.pathname));
		ctx.waitUntil(caches.default.put(key, response.clone()));
		return response;
	},
};
