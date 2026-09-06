import type { Metadata } from "next";
import { DocsShell } from "../_components/docs-shell";
import { MarkdownDocument } from "../_components/markdown-document";
import { readRepositoryDocument } from "../_lib/read-document";

export const metadata: Metadata = {
  title: "服务器部署 | SentryOrbit",
  description: "SentryOrbit 服务端、PostgreSQL、systemd、前端代理与自动部署说明。"
};

export default function ServerDocsPage() {
  return (
    <DocsShell current="server">
      <MarkdownDocument source={readRepositoryDocument("docs/server-deployment.md")} />
    </DocsShell>
  );
}
