import { BookOpen, ChevronRight, Download, ServerCog, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

const navigation = [
  { href: "/docs/client", label: "客户端操作指南", icon: BookOpen },
  { href: "/docs/server", label: "服务器部署", icon: ServerCog }
];

export function DocsShell({ children, current }: { children: ReactNode; current?: "client" | "server" }) {
  return (
    <main className="relative min-h-screen overflow-hidden">
      <div className="star-field pointer-events-none fixed inset-0 opacity-55" />
      <header className="relative border-b border-white/10 bg-[#050816]/80 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-5 sm:px-8">
          <a href="/" className="flex items-center gap-3 text-white transition hover:text-[#A9D3FF]">
            <span className="flex size-10 items-center justify-center rounded-2xl border border-[#2388FF]/35 bg-[#2388FF]/10 text-[#8DC7FF]">
              <Sparkles className="size-5" />
            </span>
            <span className="font-orbitron text-sm font-semibold tracking-[0.28em]">SENTRYORBIT</span>
          </a>
          <a
            href="/download/latest"
            className="primary-glow inline-flex items-center gap-2 rounded-full bg-[#2388FF] px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-[#4AA0FF] focus:outline-none focus:ring-2 focus:ring-[#8BC4FF]"
          >
            <Download className="size-4" />
            <span className="hidden sm:inline">下载客户端</span>
            <span className="sm:hidden">下载</span>
          </a>
        </div>
      </header>

      <div className="relative mx-auto grid max-w-7xl gap-8 px-5 py-10 sm:px-8 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside className="lg:sticky lg:top-8 lg:self-start">
          <a href="/docs" className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
            文档中心
            <ChevronRight className="size-4 text-[#6F86AA]" />
          </a>
          <nav className="glass-panel flex gap-2 rounded-2xl p-2 lg:flex-col" aria-label="文档导航">
            {navigation.map((item) => {
              const Icon = item.icon;
              const active = current && item.href.endsWith(current);
              return (
                <a
                  key={item.href}
                  href={item.href}
                  className={`flex flex-1 items-center gap-3 rounded-xl px-4 py-3 text-sm transition lg:flex-none ${
                    active
                      ? "bg-[#2388FF]/18 text-white"
                      : "text-[#AFC0DC] hover:bg-white/[0.05] hover:text-white"
                  }`}
                >
                  <Icon className={`size-4 shrink-0 ${active ? "text-[#70B6FF]" : "text-[#738AAE]"}`} />
                  {item.label}
                </a>
              );
            })}
          </nav>
        </aside>

        <div className="min-w-0">{children}</div>
      </div>
    </main>
  );
}
