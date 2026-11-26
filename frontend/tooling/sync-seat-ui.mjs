import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const destination = fileURLToPath(new URL("../src/components/ui/seat/", import.meta.url));
const manifest = join(destination, "source.json");
const upstream = "https://github.com/xiaqijun/GloryNavy_Seat";
const files = {
  "web/src/components/ui/button.tsx": "button.tsx",
  "web/src/components/ui/card.tsx": "card.tsx",
  "web/src/components/ui/dialog.tsx": "dialog.tsx",
  "web/src/components/ui/dialog.css": "dialog.css",
  "web/src/components/ui/form-dialog.tsx": "form-dialog.tsx",
  "web/src/components/ui/icon-action.tsx": "icon-action.tsx",
  "web/src/components/ui/select.tsx": "select.tsx",
  "web/src/components/ui/select.css": "select.css",
  "web/src/components/ui/toast.tsx": "toast.tsx",
  "web/src/components/ui/toast-context.ts": "toast-context.ts",
  "web/src/components/ui/toast.css": "toast.css",
  "web/src/components/search-input.tsx": "search-input.tsx",
  "web/src/components/search-input.css": "search-input.css",
};

const [checkout, requestedRevision, ...flags] = process.argv.slice(2);
if (!checkout || !requestedRevision || flags.some((flag) => flag !== "--check")) {
  throw new Error("Usage: node tooling/sync-seat-ui.mjs <SeAT checkout> <revision> [--check]");
}

const git = (...args) => execFileSync("git", ["-C", checkout, ...args]);
const revision = git("rev-parse", "--verify", "--end-of-options", `${requestedRevision}^{commit}`).toString().trim();
const sources = Object.entries(files).map(([source, name]) => ({
  path: join(destination, name),
  content: git("show", `${revision}:${source}`),
}));
const record = `${JSON.stringify({ upstream, revision }, null, 2)}\n`;

if (flags.includes("--check")) {
  for (const { path, content } of sources) {
    const local = readFileSync(path, "utf8").replaceAll("\r\n", "\n");
    if (local !== content.toString().replaceAll("\r\n", "\n")) throw new Error(`SeAT source differs: ${path}`);
  }
  if (readFileSync(manifest, "utf8").replaceAll("\r\n", "\n") !== record) throw new Error("SeAT source revision differs");
} else {
  mkdirSync(destination, { recursive: true });
  for (const { path, content } of sources) writeFileSync(path, content);
  writeFileSync(manifest, record);
}

console.log(`${Object.keys(files).length} SeAT source files at ${revision}`);
