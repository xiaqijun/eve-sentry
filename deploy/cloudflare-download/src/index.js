const ASSET_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*\.zip$/;

function githubReleaseUrl(env, assetName) {
  const owner = encodeURIComponent(env.GITHUB_OWNER || "xiaqijun");
  const repo = encodeURIComponent(env.GITHUB_REPO || "eve-sentry");
  return `https://github.com/${owner}/${repo}/releases/latest/download/${encodeURIComponent(assetName)}`;
}

function responseHeaders(source, cacheSeconds) {
  const headers = new Headers(source);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", `public, max-age=${cacheSeconds}, s-maxage=${cacheSeconds}`);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.delete("Set-Cookie");
  return headers;
}

async function proxyRelease(request, env, target, cacheSeconds) {
  const originHeaders = new Headers();
  const range = request.headers.get("Range");
  if (range) originHeaders.set("Range", range);
  originHeaders.set("User-Agent", "EVE-Sentry-Download-Worker/1.0");

  const upstream = await fetch(target, {
    headers: originHeaders,
    redirect: "follow",
    cf: {
      cacheEverything: !range,
      cacheKey: request.url,
      cacheTtl: cacheSeconds,
    },
  });
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders(upstream.headers, cacheSeconds),
  });
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405 });
    }
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/health") {
      return Response.json({ ok: true, service: "eve-sentry-download" });
    }
    if (url.pathname === "/latest.json") {
      const owner = encodeURIComponent(env.GITHUB_OWNER || "xiaqijun");
      const repo = encodeURIComponent(env.GITHUB_REPO || "eve-sentry");
      const target = `https://github.com/${owner}/${repo}/releases/latest/download/latest.json`;
      return proxyRelease(request, env, target, 60);
    }
    if (url.pathname.startsWith("/download/")) {
      const assetName = decodeURIComponent(url.pathname.slice("/download/".length));
      if (!ASSET_NAME.test(assetName)) {
        return new Response("Invalid asset", { status: 400 });
      }
      return proxyRelease(request, env, githubReleaseUrl(env, assetName), 2592000);
    }
    return new Response("Not found", { status: 404 });
  },
};
