import assert from "node:assert/strict";
import test from "node:test";

import worker from "./index.js";

const env = {
  GITHUB_OWNER: "xiaqijun",
  GITHUB_REPO: "eve-sentry",
};
const assetName = "EVE-Sentry-Monitor-ONNX-program-1.0.31.zip";

function invalidUpstreamResponse() {
  return {
    body: null,
    headers: new Headers(),
    status: 0,
    statusText: "",
  };
}

test("retries an invalid cached response with the exact release URL", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (target, options) => {
    calls.push({ target: String(target), options });
    if (calls.length === 1) return invalidUpstreamResponse();
    return new Response(new Uint8Array([1, 2, 3]), {
      status: 206,
      headers: { "Content-Range": "bytes 0-2/3" },
    });
  };
  try {
    const request = new Request(
      `https://download.example/download/${assetName}`,
      { headers: { Range: "bytes=0-2" } },
    );
    const response = await worker.fetch(request, env);

    assert.equal(response.status, 206);
    assert.equal(calls.length, 2);
    assert.match(calls[0].target, /releases\/download\/v1\.0\.31\//);
    assert.match(calls[1].target, /releases\/download\/v1\.0\.31\//);
    assert.equal(calls[1].options.headers.get("Cache-Control"), "no-cache");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("returns a stable 502 when both upstream attempts are invalid", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => invalidUpstreamResponse();
  try {
    const request = new Request(
      `https://download.example/download/${assetName}`,
      { headers: { Range: "bytes=0-2" } },
    );
    const response = await worker.fetch(request, env);

    assert.equal(response.status, 502);
    assert.equal(response.headers.get("Cache-Control"), "no-store");
    assert.equal(await response.text(), "Release upstream unavailable");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("uses the signed manifest release query for model fallback", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (target, options) => {
    calls.push({ target: String(target), options });
    if (calls.length === 1) return invalidUpstreamResponse();
    return new Response(new Uint8Array([1]), { status: 206 });
  };
  try {
    const model = "EVE-Sentry-Monitor-ONNX-models-deadbeef.zip";
    const request = new Request(
      `https://download.example/download/${model}?release=1.0.32`,
      { headers: { Range: "bytes=0-0" } },
    );
    const response = await worker.fetch(request, env);

    assert.equal(response.status, 206);
    assert.match(calls[1].target, /releases\/download\/v1\.0\.32\//);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
