import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MarkdownDocument({ source }: { source: string }) {
  return (
    <article className="glass-panel rounded-[28px] px-5 py-7 sm:px-9 sm:py-10">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => (
            <h1 className="font-orbitron text-3xl font-semibold tracking-tight text-white sm:text-4xl">{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="mt-12 scroll-mt-8 border-t border-white/10 pt-9 text-2xl font-semibold text-white">{children}</h2>
          ),
          h3: ({ children }) => <h3 className="mt-8 text-lg font-semibold text-[#E7F1FF]">{children}</h3>,
          p: ({ children }) => <p className="mt-4 text-[15px] leading-8 text-[#B5C4DE]">{children}</p>,
          ul: ({ children }) => <ul className="mt-4 list-disc space-y-2 pl-6 text-[15px] leading-7 text-[#B5C4DE]">{children}</ul>,
          ol: ({ children }) => <ol className="mt-4 list-decimal space-y-2 pl-6 text-[15px] leading-7 text-[#B5C4DE]">{children}</ol>,
          li: ({ children }) => <li className="pl-1 marker:text-[#5DA9FF]">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold text-white">{children}</strong>,
          a: ({ href, children }) => (
            <a href={href} className="font-medium text-[#70B6FF] underline decoration-[#2388FF]/40 underline-offset-4 hover:text-[#A9D3FF]">
              {children}
            </a>
          ),
          pre: ({ children }) => (
            <pre className="mt-5 overflow-x-auto rounded-2xl border border-white/10 bg-[#020712] p-5 text-sm leading-7 text-[#C9DBF6] shadow-inner">
              {children}
            </pre>
          ),
          code: ({ children, className }) =>
            className ? (
              <code className={className}>{children}</code>
            ) : (
              <code className="rounded-md border border-white/10 bg-white/[0.06] px-1.5 py-0.5 text-[0.9em] text-[#CBE3FF]">{children}</code>
            ),
          table: ({ children }) => (
            <div className="mt-5 overflow-x-auto rounded-2xl border border-white/10">
              <table className="w-full min-w-[560px] border-collapse text-left text-sm">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-[#2388FF]/12 text-white">{children}</thead>,
          th: ({ children }) => <th className="border-b border-white/10 px-4 py-3 font-semibold">{children}</th>,
          td: ({ children }) => <td className="border-b border-white/[0.07] px-4 py-3 leading-6 text-[#B5C4DE]">{children}</td>,
          blockquote: ({ children }) => (
            <blockquote className="mt-5 border-l-2 border-[#2388FF] bg-[#2388FF]/8 px-5 py-1">{children}</blockquote>
          )
        }}
      >
        {source}
      </ReactMarkdown>
    </article>
  );
}
