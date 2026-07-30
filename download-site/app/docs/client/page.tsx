import type { Metadata } from "next";
import { DocsShell } from "../_components/docs-shell";
import { MarkdownDocument } from "../_components/markdown-document";
import { readRepositoryDocument } from "../_lib/read-document";

export const metadata: Metadata = {
  title: "客户端操作指南 | SentryOrbit",
  description: "SentryOrbit Windows 客户端下载安装、界面操作、更新与常见问题。"
};

export default function ClientDocsPage() {
  return (
    <DocsShell current="client">
      <MarkdownDocument source={readRepositoryDocument("docs/client.md")} />
    </DocsShell>
  );
}
