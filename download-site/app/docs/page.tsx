import type { Metadata } from "next";
import { BookOpen, ChevronRight, ServerCog } from "lucide-react";
import { DocsShell } from "./_components/docs-shell";

export const metadata: Metadata = {
  title: "文档中心 | SentryOrbit",
  description: "SentryOrbit 客户端操作指南与服务器部署文档。"
};

const documents = [
  {
    href: "/docs/client",
    title: "客户端操作指南",
    description: "下载安装、连接服务端、选择 EVE 窗口、开始监控、开启预警和常见问题。",
    icon: BookOpen,
    accent: "border-[#2388FF]/30 bg-[#2388FF]/10 text-[#70B6FF]"
  },
  {
    href: "/docs/server",
    title: "服务器部署",
    description: "PostgreSQL、环境配置、SDE、EVE SSO、systemd、前端代理和上线验证。",
    icon: ServerCog,
    accent: "border-[#7CFFB2]/25 bg-[#7CFFB2]/8 text-[#7CFFB2]"
  }
];

export default function DocsIndexPage() {
  return (
    <DocsShell>
      <div className="mb-8 max-w-3xl">
        <p className="font-orbitron text-xs uppercase tracking-[0.28em] text-[#70B6FF]">Documentation</p>
        <h1 className="mt-4 text-4xl font-semibold tracking-tight text-white sm:text-5xl">选择你需要的文档</h1>
        <p className="mt-5 text-base leading-8 text-[#B5C4DE]">
          普通用户只需阅读客户端操作指南；服务器部署文档面向负责安装和维护服务端的管理员。
        </p>
      </div>
      <div className="grid gap-5 md:grid-cols-2">
        {documents.map((document) => {
          const Icon = document.icon;
          return (
            <a key={document.href} href={document.href} className="glass-panel group rounded-[26px] p-6 transition hover:-translate-y-1 hover:border-[#2388FF]/45">
              <div className={`flex size-12 items-center justify-center rounded-2xl border ${document.accent}`}>
                <Icon className="size-6" />
              </div>
              <h2 className="mt-6 text-2xl font-semibold text-white">{document.title}</h2>
              <p className="mt-3 text-sm leading-7 text-[#B5C4DE]">{document.description}</p>
              <span className="mt-7 inline-flex items-center gap-2 text-sm font-semibold text-[#70B6FF]">
                打开文档
                <ChevronRight className="size-4 transition group-hover:translate-x-1" />
              </span>
            </a>
          );
        })}
      </div>
    </DocsShell>
  );
}
