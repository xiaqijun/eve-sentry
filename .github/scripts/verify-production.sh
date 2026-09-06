#!/usr/bin/env bash
set -euo pipefail

base_url="${DOWNLOAD_SITE_URL:-https://evesentrydownload.kisectool.com}"
base_url="${base_url%/}"

for path in / /docs/client /docs/server; do
  curl -fsS --retry 10 --retry-delay 2 --retry-all-errors \
    "${base_url}${path}" >/dev/null
done

curl -fsS --retry 10 --retry-delay 2 --retry-all-errors \
  "${base_url}/" | grep -q "SENTRYORBIT"

curl -fsS --retry 10 --retry-delay 2 --retry-all-errors \
  "${base_url}/health" >health.json
node -e "const p=require('./health.json');if(p.ok!==true||p.service!=='eve-sentry-download')process.exit(1)"

curl -fsS --retry 10 --retry-delay 2 --retry-all-errors \
  "${base_url}/latest.json" >latest.json
node - <<'NODE'
const p = require('./latest.json');
if (!/^\d+\.\d+\.\d+$/.test(String(p.version || ''))) process.exit(1);
if (p.filename !== `EVE-Sentry-Monitor-ONNX-program-${p.version}.zip`) process.exit(1);
if (!(p.size > 0) || !/^[0-9a-f]{64}$/i.test(p.sha256)) process.exit(1);
if (p.signing_key_id !== 'eve-sentry-release-v1' || !p.signature) process.exit(1);
NODE

release_version="$(node -p "require('./latest.json').version")"

redirect_status="$(curl -sS -D redirect.headers -o /dev/null -w '%{http_code}' \
  "${base_url}/download/latest")"
test "$redirect_status" = "302"
grep -Eiq "^location: ${base_url}/download/EVE-Sentry-Monitor-ONNX-${release_version}\.zip" \
  redirect.headers

range_status="$(curl -fsS -L --range 0-1023 --max-filesize 2048 \
  -D range.headers -o /dev/null -w '%{http_code}' \
  "${base_url}/download/latest")"
test "$range_status" = "206"
grep -Eiq '^content-range: bytes 0-1023/[1-9][0-9]*' range.headers
