const ASSET_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*\.zip$/;
const RELEASE_VERSION = /^\d+\.\d+\.\d+(?:[+-][0-9A-Za-z.-]+)?$/;

function githubReleaseUrl(env, assetName) {
  const owner = encodeURIComponent(env.GITHUB_OWNER || "xiaqijun");
  const repo = encodeURIComponent(env.GITHUB_REPO || "eve-sentry");
  return `https://github.com/${owner}/${repo}/releases/latest/download/${encodeURIComponent(assetName)}`;
}

function taggedReleaseUrl(env, request, fallbackUrl) {
  const requestUrl = new URL(request.url);
  const pathname = requestUrl.pathname;
  if (!pathname.startsWith("/download/")) return fallbackUrl;
  const assetName = decodeURIComponent(pathname.slice("/download/".length));
  const match = assetName.match(/-(\d+\.\d+\.\d+(?:[+-][0-9A-Za-z.-]+)?)\.zip$/);
  const releaseVersion = String(requestUrl.searchParams.get("release") || "").trim();
  const version = RELEASE_VERSION.test(releaseVersion)
    ? releaseVersion
    : match?.[1];
  if (!version) return fallbackUrl;
  const owner = encodeURIComponent(env.GITHUB_OWNER || "xiaqijun");
  const repo = encodeURIComponent(env.GITHUB_REPO || "eve-sentry");
  return `https://github.com/${owner}/${repo}/releases/download/v${encodeURIComponent(version)}/${encodeURIComponent(assetName)}`;
}

function latestManifestUrl(env) {
  const owner = encodeURIComponent(env.GITHUB_OWNER || "xiaqijun");
  const repo = encodeURIComponent(env.GITHUB_REPO || "eve-sentry");
  return `https://github.com/${owner}/${repo}/releases/latest/download/latest.json`;
}

async function redirectToLatestClient(request, env) {
  const upstream = await fetch(latestManifestUrl(env), {
    headers: { "User-Agent": "EVE-Sentry-Download-Worker/1.0" },
    redirect: "follow",
    cf: {
      cacheEverything: true,
      cacheTtl: 60,
    },
  });
  if (!upstream.ok) {
    return new Response("Latest release unavailable", { status: 502 });
  }

  let manifest;
  try {
    manifest = await upstream.json();
  } catch {
    return new Response("Invalid latest release manifest", { status: 502 });
  }
  const version = String(manifest?.version || "").trim().replace(/^v/, "");
  if (!RELEASE_VERSION.test(version)) {
    return new Response("Invalid latest release version", { status: 502 });
  }

  const assetName = `EVE-Sentry-Monitor-ONNX-${version}.zip`;
  const target = new URL(`/download/${encodeURIComponent(assetName)}`, request.url);
  return new Response(null, {
    status: 302,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "public, max-age=60, s-maxage=60",
      Location: target.toString(),
      "X-Content-Type-Options": "nosniff",
    },
  });
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

  let upstream;
  try {
    upstream = await fetch(target, {
      headers: originHeaders,
      redirect: "follow",
      cf: {
        cacheEverything: !range,
        cacheKey: request.url,
        cacheTtl: cacheSeconds,
      },
    });
  } catch (error) {
    console.error("Release upstream fetch failed", String(error));
  }

  const validStatus = (response) => (
    response
    && Number.isInteger(response.status)
    && response.status >= 200
    && response.status <= 599
  );
  if (!validStatus(upstream) || upstream.status >= 500) {
    const retryHeaders = new Headers(originHeaders);
    retryHeaders.set("Cache-Control", "no-cache");
    try {
      upstream = await fetch(taggedReleaseUrl(env, request, target), {
        headers: retryHeaders,
        redirect: "follow",
        cf: {
          cacheEverything: false,
          cacheTtl: 0,
        },
      });
    } catch (error) {
      console.error("Release upstream retry failed", String(error));
    }
  }

  if (!validStatus(upstream) || upstream.status >= 500) {
    return new Response("Release upstream unavailable", {
      status: 502,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
        "Content-Type": "text/plain; charset=utf-8",
        "Retry-After": "5",
        "X-Content-Type-Options": "nosniff",
      },
    });
  }
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
    if (url.pathname === "/health") {
      return Response.json({ ok: true, service: "eve-sentry-download" });
    }
    if (url.pathname === "/latest.json") {
      return proxyRelease(request, env, latestManifestUrl(env), 60);
    }
    if (url.pathname === "/download/latest") {
      return redirectToLatestClient(request, env);
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
