# SentryOrbit Download Site

Standalone Next.js 15 download landing page for the SentryOrbit Windows client.

## Development

~~~powershell
npm install
npm run dev
~~~

The local dev server uses `http://127.0.0.1:4174`.

## Build

~~~powershell
npm run build
~~~

The site is configured with `output: "export"` and writes static assets to `out/`.

## Deploy

The existing Cloudflare download Worker serves the exported site and keeps the
release API routes handled by Worker code:

~~~powershell
npm run build
cd ..\deploy\cloudflare-download
npx wrangler deploy
~~~

The custom domain root serves the landing page. `/health`, `/latest.json`, and
`/download/*` continue to run through the Worker before static asset routing.
Release downloads validate the upstream HTTP status before constructing the
response. Versioned assets use an exact GitHub release tag from the first
request. Invalid edge responses and upstream 5xx errors are retried once
without cache; persistent failures return a non-cacheable 502.

## Release Data

The page reads the public update manifest from:

~~~text
https://evesentrydownload.kisectool.com/latest.json
~~~

The primary download button always points to the stable unversioned URL:

~~~text
https://evesentrydownload.kisectool.com/download/latest
~~~
