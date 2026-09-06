"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  BellRing,
  BookOpen,
  ChevronDown,
  CircleHelp,
  Download,
  FileCheck2,
  Gauge,
  KeyRound,
  MonitorCheck,
  RadioTower,
  ScanLine,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
  Volume2
} from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import clsx from "clsx";

const DOWNLOAD_URL = "https://evesentrydownload.kisectool.com/download/latest";
const MANIFEST_URL = "https://evesentrydownload.kisectool.com/latest.json";
const DOCS_URL = "/docs/client";

type ReleaseManifest = {
  version?: string;
  sha256?: string;
  size?: number;
  filename?: string;
  released_at?: string;
  signature_algorithm?: string;
  signing_key_id?: string;
  components?: {
    models?: {
      version?: string;
      sha256?: string;
      size?: number;
      filename?: string;
    };
  };
};

type ManifestState =
  | { status: "loading"; data: null }
  | { status: "ready"; data: ReleaseManifest }
  | { status: "error"; data: null };

const featureCards = [
  {
    title: "星域敌对预警",
    description: "从成员列表识别敌对角色，聚合到服务端态势视图，让值守人员更早看见风险。",
    icon: RadioTower,
    accent: "from-[#2388FF] to-[#6EE7FF]"
  },
  {
    title: "多渠道消息推送",
    description: "桌面端、浮窗和服务端事件流协同工作，避免单点提示丢失。",
    icon: BellRing,
    accent: "from-[#FF3B57] to-[#FF9B6A]"
  },
  {
    title: "低占用后台运行",
    description: "按窗口变化调度 OCR，弱网下保留最新有效快照，减少重复建连与资源浪费。",
    icon: Gauge,
    accent: "from-[#7CFFB2] to-[#2388FF]"
  }
];

const changelog = [
  "新增 Cloudflare 固定下载入口与 HTTP Range 断点续传验证。",
  "客户端更新清单启用 Ed25519 签名校验，程序包继续校验 SHA256。",
  "OCR 模型拆分为独立组件，模型未变化时不重复下载。",
  "自动更新完成后清理临时包、旧备份和遗留安装文件。"
];

const setupSteps = [
  {
    title: "下载并解压",
    description: "下载最新版 Windows 客户端，解压完整压缩包，不要只复制其中的 EXE 文件。"
  },
  {
    title: "连接服务端",
    description: "启动 EVE-Sentry-Monitor.exe，在左侧填写管理员提供的服务端地址和以 eve_ 开头的设备密钥。"
  },
  {
    title: "选择游戏窗口",
    description: "先登录 EVE 并打开本地频道成员列表，再从“目标窗口”下拉框选择对应角色。"
  },
  {
    title: "框选识别区域",
    description: "点击“选择区域”框住成员列表，然后点击“预览”，确认画面没有混入聊天正文或其他窗口。"
  },
  {
    title: "开始监控",
    description: "确认运行状态没有异常后点击“开始监控”，客户端会识别所选窗口并向服务端上报。"
  },
  {
    title: "开启预警",
    description: "需要接收其他节点消息时点击“开启预警”，顶部浮窗会显示状态并按设置播放告警声音。"
  }
];

const commonIssues = [
  {
    title: "目标窗口为空",
    description: "确认 EVE 已进入角色、没有最小化且使用窗口化或无边框模式，然后点击目标窗口右侧的刷新图标。"
  },
  {
    title: "预览区域不正确",
    description: "重新点击“选择区域”，只框选本地频道成员列表；窗口大小或显示器缩放变化后也建议重新框选。"
  },
  {
    title: "无法开始监控",
    description: "依次检查运行状态中的服务端、身份、OCR、窗口和区域。密钥失效时请重新创建或联系管理员。"
  },
  {
    title: "有红色预警但没有声音",
    description: "确认左侧“告警声音”已开启、Windows 没有静音，并检查播放间隔和播放次数。"
  }
];

const formatBytes = (value?: number) => {
  if (!value || value <= 0) return "清单加载后显示";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
};

const formatDate = (value?: string) => {
  if (!value) return "清单加载后显示";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).format(new Date(value));
  } catch {
    return value;
  }
};

const shortHash = (value?: string) => {
  if (!value) return "清单加载后显示";
  return `${value.slice(0, 12)}...${value.slice(-12)}`;
};

function RadarVisual({ reduceMotion }: { reduceMotion: boolean }) {
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center overflow-hidden">
      <div className="orbital-grid absolute inset-x-[-8%] top-[-8rem] h-[56rem] opacity-70" />
      <div className="relative size-[30rem] max-w-[84vw] rounded-full border border-[#2388FF]/20">
        {[1, 2, 3].map((ring) => (
          <div
            key={ring}
            className="absolute rounded-full border border-[#2388FF]/15"
            style={{
              inset: `${ring * 14}%`
            }}
          />
        ))}
        <div className="absolute left-1/2 top-0 h-full w-px bg-gradient-to-b from-transparent via-[#2388FF]/40 to-transparent" />
        <div className="absolute top-1/2 h-px w-full bg-gradient-to-r from-transparent via-[#2388FF]/40 to-transparent" />
        <div
          className="absolute inset-0 rounded-full"
          style={{
            animation: reduceMotion ? undefined : "radar-sweep 7s linear infinite",
            background:
              "conic-gradient(from 0deg, rgba(35,136,255,0.38), rgba(35,136,255,0.12) 14deg, transparent 54deg)"
          }}
        />
        <div
          className="absolute left-1/2 top-1/2 size-5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[#2388FF]"
          style={{
            boxShadow: "0 0 40px rgba(35,136,255,.85)",
            animation: reduceMotion ? undefined : "soft-pulse 3.6s ease-in-out infinite"
          }}
        />
      </div>
    </div>
  );
}

function ProductPreview() {
  const hostiles = [
    { name: "K-6K16", level: "高", count: 7 },
    { name: "9-4RP2", level: "中", count: 3 },
    { name: "J5A-IX", level: "低", count: 1 }
  ];

  return (
    <div className="glass-panel relative mx-auto w-full max-w-5xl overflow-hidden rounded-[28px] p-3">
      <div className="rounded-[22px] border border-white/10 bg-[#06101F]/92">
        <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="size-3 rounded-full bg-[#FF3B57]" />
            <span className="size-3 rounded-full bg-[#FFC857]" />
            <span className="size-3 rounded-full bg-[#49E28D]" />
          </div>
          <div className="font-orbitron text-xs tracking-[0.28em] text-[#8CA3C7]">SENTRYORBIT CONSOLE</div>
          <div className="hidden items-center gap-2 text-xs text-[#8CA3C7] sm:flex">
            <Activity className="size-4 text-[#49E28D]" />
            实时在线
          </div>
        </div>
        <div className="grid gap-4 p-4 md:grid-cols-[1.35fr_0.8fr] md:p-6">
          <div className="rounded-2xl border border-[#2388FF]/18 bg-[#07172D] p-4">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.24em] text-[#8CA3C7]">Tactical Orbit</p>
                <h3 className="mt-1 text-xl font-semibold text-white">星域态势</h3>
              </div>
              <span className="rounded-full border border-[#2388FF]/30 bg-[#2388FF]/10 px-3 py-1 text-xs text-[#B9D8FF]">
                12 个节点
              </span>
            </div>
            <div className="relative h-72 overflow-hidden rounded-2xl bg-[radial-gradient(circle_at_center,rgba(35,136,255,.18),transparent_50%),linear-gradient(180deg,#091B33,#030914)]">
              <div className="absolute inset-8 rounded-full border border-[#2388FF]/15" />
              <div className="absolute inset-20 rounded-full border border-[#2388FF]/10" />
              {[
                ["left-[46%] top-[42%]", "bg-[#2388FF]"],
                ["left-[24%] top-[34%]", "bg-[#49E28D]"],
                ["left-[68%] top-[26%]", "bg-[#FF3B57]"],
                ["left-[70%] top-[62%]", "bg-[#2388FF]"],
                ["left-[34%] top-[68%]", "bg-[#FFC857]"]
              ].map(([position, color]) => (
                <span
                  key={position}
                  className={clsx(
                    "absolute size-4 rounded-full shadow-[0_0_24px_currentColor]",
                    position,
                    color
                  )}
                />
              ))}
              <svg className="absolute inset-0 h-full w-full" aria-hidden="true">
                <path d="M270 145 C365 80 440 95 515 95" stroke="rgba(255,59,87,.65)" strokeWidth="2" fill="none" />
                <path d="M270 145 C190 122 160 105 124 114" stroke="rgba(35,136,255,.45)" strokeWidth="1.5" fill="none" />
                <path d="M270 145 C340 215 410 210 520 214" stroke="rgba(35,136,255,.35)" strokeWidth="1.5" fill="none" />
              </svg>
              <div className="absolute right-5 top-5 w-52 rounded-2xl border border-[#FF3B57]/30 bg-[#120813]/80 p-3 shadow-[0_0_28px_rgba(255,59,87,.22)] backdrop-blur">
                <div className="flex items-center gap-3">
                  <div className="flex size-11 items-center justify-center rounded-xl bg-gradient-to-br from-[#FF3B57] to-[#2388FF] font-orbitron text-sm font-bold">
                    NK
                  </div>
                  <div>
                    <p className="font-semibold text-white">Nari Kestrel</p>
                    <p className="text-xs text-[#FF9AAA]">威胁度 91</p>
                  </div>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-[#B8C9E6]">
                  <span>军团：RAVEN</span>
                  <span>联盟：NULL-X</span>
                </div>
              </div>
            </div>
          </div>
          <div className="space-y-3">
            {hostiles.map((item) => (
              <div key={item.name} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
                <div className="flex items-center justify-between">
                  <span className="font-orbitron text-sm text-white">{item.name}</span>
                  <span
                    className={clsx(
                      "rounded-full px-2.5 py-1 text-xs",
                      item.level === "高"
                        ? "bg-[#FF3B57]/15 text-[#FF8FA0]"
                        : item.level === "中"
                          ? "bg-[#FFC857]/15 text-[#FFD88A]"
                          : "bg-[#2388FF]/15 text-[#A9D3FF]"
                    )}
                  >
                    {item.level}威胁
                  </span>
                </div>
                <div className="mt-4 flex items-end justify-between">
                  <span className="text-sm text-[#8CA3C7]">敌对人数</span>
                  <span className="font-orbitron text-3xl font-semibold text-white">{item.count}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function ClientGuide() {
  return (
    <section id="client-guide" className="relative mx-auto max-w-6xl scroll-mt-8 px-5 py-20 sm:px-8">
      <motion.div
        initial={{ opacity: 0, y: 28 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.12 }}
        transition={{ duration: 0.55, ease: "easeOut" }}
      >
        <div className="mx-auto max-w-3xl text-center">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-[#2388FF]/25 bg-[#2388FF]/10 px-4 py-2 text-sm text-[#A9D3FF]">
            <BookOpen className="size-4" />
            客户端操作指南
          </div>
          <h2 className="text-3xl font-semibold tracking-tight text-white sm:text-4xl">
            从下载到开始预警，都在这一页
          </h2>
          <p className="mt-4 text-base leading-8 text-[#B5C4DE]">
            Windows 便携客户端无需安装 Python、OCR 模型或配置环境变量。按下面顺序操作即可。
          </p>
        </div>

        <div className="mt-10 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {setupSteps.map((step, index) => (
            <article key={step.title} className="glass-panel rounded-[24px] p-5">
              <div className="flex items-center justify-between">
                <span className="font-orbitron text-sm tracking-[0.2em] text-[#70B6FF]">
                  {String(index + 1).padStart(2, "0")}
                </span>
                {index === 0 ? (
                  <Download className="size-5 text-[#8DC7FF]" />
                ) : index === 1 ? (
                  <KeyRound className="size-5 text-[#8DC7FF]" />
                ) : index < 4 ? (
                  <ScanLine className="size-5 text-[#8DC7FF]" />
                ) : (
                  <MonitorCheck className="size-5 text-[#8DC7FF]" />
                )}
              </div>
              <h3 className="mt-5 text-lg font-semibold text-white">{step.title}</h3>
              <p className="mt-2 text-sm leading-7 text-[#B5C4DE]">{step.description}</p>
            </article>
          ))}
        </div>

        <div className="mt-6 grid gap-6 lg:grid-cols-[1.05fr_0.95fr]">
          <div className="glass-panel rounded-[28px] p-6 sm:p-7">
            <div className="flex items-center gap-3 text-[#A9D3FF]">
              <TerminalSquare className="size-5" />
              <span className="font-orbitron text-sm uppercase tracking-[0.24em]">Controls</span>
            </div>
            <h3 className="mt-4 text-2xl font-semibold text-white">监控和预警是两个独立开关</h3>
            <div className="mt-6 grid gap-4 sm:grid-cols-2">
              <div className="rounded-2xl border border-[#2388FF]/25 bg-[#2388FF]/10 p-5">
                <MonitorCheck className="size-6 text-[#70B6FF]" />
                <h4 className="mt-4 font-semibold text-white">开始监控</h4>
                <p className="mt-2 text-sm leading-7 text-[#B5C4DE]">
                  识别本机当前所选 EVE 窗口，并把结果发送到服务端；不会自动打开预警浮窗。
                </p>
              </div>
              <div className="rounded-2xl border border-[#FF3B57]/25 bg-[#FF3B57]/10 p-5">
                <BellRing className="size-6 text-[#FF8FA0]" />
                <h4 className="mt-4 font-semibold text-white">开启预警</h4>
                <p className="mt-2 text-sm leading-7 text-[#B5C4DE]">
                  接收各监控节点的消息并显示顶部浮窗；需要完整功能时请同时开启两个按钮。
                </p>
              </div>
            </div>
            <div className="mt-4 rounded-2xl border border-white/10 bg-white/[0.04] p-5 text-sm leading-7 text-[#B5C4DE]">
              <p><strong className="text-white">浮窗颜色：</strong>绿色表示安全，红色表示发现敌对目标，红色方框会显示当前人数。</p>
              <p className="mt-2"><strong className="text-white">告警声音：</strong>左侧可开关声音，并设置播放间隔与播放次数。</p>
            </div>
          </div>

          <div className="glass-panel rounded-[28px] p-6 sm:p-7">
            <div className="flex items-center gap-3 text-[#A9D3FF]">
              <CircleHelp className="size-5" />
              <span className="font-orbitron text-sm uppercase tracking-[0.24em]">Help</span>
            </div>
            <h3 className="mt-4 text-2xl font-semibold text-white">常见问题</h3>
            <div className="mt-5 space-y-3">
              {commonIssues.map((item) => (
                <details key={item.title} className="group rounded-2xl border border-white/10 bg-white/[0.04] p-4">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-4 text-sm font-semibold text-white">
                    {item.title}
                    <ChevronDown className="size-4 shrink-0 text-[#8CA3C7] transition group-open:rotate-180" />
                  </summary>
                  <p className="mt-3 text-sm leading-7 text-[#B5C4DE]">{item.description}</p>
                </details>
              ))}
            </div>
            <div className="mt-4 flex gap-3 rounded-2xl border border-[#FFC857]/20 bg-[#FFC857]/[0.07] p-4">
              <Volume2 className="mt-0.5 size-5 shrink-0 text-[#FFD88A]" />
              <p className="text-sm leading-7 text-[#C9D6EA]">
                仍无法解决时，点击客户端左下角的“导出诊断包”，把文件和问题发生时间一起交给管理员。
              </p>
            </div>
          </div>
        </div>

        <div className="mt-6 flex flex-col items-center justify-between gap-5 rounded-[24px] border border-[#2388FF]/25 bg-[#2388FF]/10 p-6 sm:flex-row">
          <div>
            <h3 className="text-lg font-semibold text-white">更新客户端</h3>
            <p className="mt-2 text-sm leading-7 text-[#B5C4DE]">
              客户端会自动检查新版本，也可点击左下角“检查更新”；发现更新后按提示选择“安装并重启”。
            </p>
          </div>
          <a
            href={DOWNLOAD_URL}
            className="primary-glow inline-flex shrink-0 items-center gap-2 rounded-full bg-[#2388FF] px-6 py-3 text-sm font-semibold text-white transition hover:-translate-y-0.5 hover:bg-[#4AA0FF] focus:outline-none focus:ring-2 focus:ring-[#8BC4FF]"
          >
            <Download className="size-4" />
            下载最新版
          </a>
        </div>
      </motion.div>
    </section>
  );
}

function DownloadInfo({ manifest }: { manifest: ManifestState }) {
  const data = manifest.status === "ready" ? manifest.data : undefined;
  const signature = data?.signature_algorithm
    ? `${data.signature_algorithm.toUpperCase()} · ${data.signing_key_id ?? "release key"}`
    : "Ed25519 签名校验";

  const rows = [
    ["软件版本", data?.version ? `v${data.version}` : manifest.status === "error" ? "读取失败" : "加载中"],
    ["支持系统", "Windows 10/11 x64"],
    ["文件大小", formatBytes(data?.size)],
    ["发布时间", formatDate(data?.released_at)],
    ["SHA256", shortHash(data?.sha256)],
    ["签名", signature]
  ];

  return (
    <section id="download-info" className="mx-auto max-w-6xl px-5 py-20 sm:px-8">
      <motion.div
        initial={{ opacity: 0, y: 28 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.3 }}
        transition={{ duration: 0.55, ease: "easeOut" }}
        className="glass-panel overflow-hidden rounded-[28px]"
      >
        <div className="grid gap-0 lg:grid-cols-[0.9fr_1.1fr]">
          <div className="border-b border-white/10 p-7 lg:border-b-0 lg:border-r">
            <div className="mb-5 flex items-center gap-3 text-[#A9D3FF]">
              <FileCheck2 className="size-5" />
              <span className="font-orbitron text-sm uppercase tracking-[0.28em]">Release</span>
            </div>
            <h2 className="text-3xl font-semibold tracking-tight text-white md:text-4xl">
              下载信息透明可校验
            </h2>
            <p className="mt-4 text-base leading-8 text-[#B5C4DE]">
              页面会读取 Cloudflare 最新清单展示版本与校验摘要。清单暂时不可用时，主下载入口仍然可点击。
            </p>
            <a
              href={DOWNLOAD_URL}
              className="primary-glow mt-7 inline-flex items-center gap-2 rounded-full bg-[#2388FF] px-6 py-3 text-sm font-semibold text-white transition hover:-translate-y-0.5 hover:bg-[#4AA0FF] focus:outline-none focus:ring-2 focus:ring-[#8BC4FF]"
            >
              <Download className="size-4" />
              最新版本下载
            </a>
          </div>
          <div className="p-5 sm:p-7">
            <div className="grid gap-3 sm:grid-cols-2">
              {rows.map(([label, value]) => (
                <div key={label} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
                  <p className="text-xs uppercase tracking-[0.18em] text-[#7E94B8]">{label}</p>
                  <p className="mt-2 break-words text-sm font-medium text-white">{value}</p>
                </div>
              ))}
            </div>
            <details className="group mt-4 rounded-2xl border border-white/10 bg-white/[0.04] p-4">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-4 text-sm font-semibold text-white">
                更新日志
                <ChevronDown className="size-4 text-[#8CA3C7] transition group-open:rotate-180" />
              </summary>
              <ul className="mt-4 space-y-3 text-sm leading-6 text-[#B5C4DE]">
                {changelog.map((item) => (
                  <li key={item} className="flex gap-3">
                    <span className="mt-2 size-1.5 shrink-0 rounded-full bg-[#2388FF]" />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </details>
          </div>
        </div>
      </motion.div>
    </section>
  );
}

export default function Home() {
  const reduceMotion = useReducedMotion();
  const [manifest, setManifest] = useState<ManifestState>({ status: "loading", data: null });

  useEffect(() => {
    let cancelled = false;

    fetch(MANIFEST_URL, { cache: "no-store" })
      .then((response) => {
        if (!response.ok) {
          throw new Error("manifest unavailable");
        }
        return response.json() as Promise<ReleaseManifest>;
      })
      .then((data) => {
        if (!cancelled) {
          setManifest({ status: "ready", data });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setManifest({ status: "error", data: null });
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const headline = useMemo(() => ["SentryOrbit", "EVE", "星域预警客户端下载"], []);

  return (
    <main className="relative min-h-screen overflow-hidden">
      <div className="star-field pointer-events-none fixed inset-0 opacity-70" />
      <RadarVisual reduceMotion={Boolean(reduceMotion)} />

      <section className="relative mx-auto flex min-h-screen max-w-7xl flex-col px-5 pb-14 pt-8 sm:px-8">
        <header className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-2xl border border-[#2388FF]/35 bg-[#2388FF]/10 text-[#8DC7FF] shadow-[0_0_24px_rgba(35,136,255,.24)]">
              <Sparkles className="size-5" />
            </div>
            <span className="font-orbitron text-sm font-semibold tracking-[0.32em] text-white">SENTRYORBIT</span>
          </div>
          <a
            href={DOCS_URL}
            className="hidden rounded-full border border-white/12 px-4 py-2 text-sm text-[#C6D7F2] transition hover:border-[#2388FF]/60 hover:text-white focus:outline-none focus:ring-2 focus:ring-[#8BC4FF] sm:inline-flex"
          >
            查看文档
          </a>
        </header>

        <div className="flex flex-1 flex-col items-center justify-center pt-16 text-center">
          <motion.div
            initial={reduceMotion ? false : "hidden"}
            animate="show"
            variants={{
              hidden: {},
              show: {
                transition: {
                  staggerChildren: 0.08
                }
              }
            }}
            className="max-w-5xl"
          >
            <div className="mb-5 inline-flex items-center gap-2 rounded-full border border-[#2388FF]/25 bg-[#2388FF]/10 px-4 py-2 text-sm text-[#A9D3FF]">
              <ShieldCheck className="size-4" />
              Windows 便携客户端 · 签名清单 · Cloudflare 加速
            </div>
            <h1 className="font-orbitron text-4xl font-semibold leading-tight tracking-[0.02em] text-white sm:text-6xl lg:text-7xl">
              {headline.map((word) => (
                <motion.span
                  key={word}
                  variants={{
                    hidden: { opacity: 0, y: 24, filter: "blur(8px)" },
                    show: { opacity: 1, y: 0, filter: "blur(0px)" }
                  }}
                  transition={{ duration: 0.55, ease: "easeOut" }}
                  className="mr-4 inline-block"
                >
                  {word}
                </motion.span>
              ))}
            </h1>
            <motion.p
              variants={{
                hidden: { opacity: 0, y: 20 },
                show: { opacity: 1, y: 0 }
              }}
              transition={{ duration: 0.55, ease: "easeOut" }}
              className="mx-auto mt-6 max-w-3xl text-base leading-8 text-[#B5C4DE] sm:text-lg"
            >
              面向 EVE Online 团队协作的第三方预警工具。下载客户端后选择游戏窗口，即可将敌对识别、在线状态和告警快照同步到服务端态势视图。
            </motion.p>
            <motion.div
              variants={{
                hidden: { opacity: 0, y: 18 },
                show: { opacity: 1, y: 0 }
              }}
              transition={{ duration: 0.55, ease: "easeOut" }}
              className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row"
            >
              <a
                href={DOWNLOAD_URL}
                className="primary-glow inline-flex w-full items-center justify-center gap-2 rounded-full bg-[#2388FF] px-7 py-3.5 text-sm font-semibold text-white transition hover:-translate-y-0.5 hover:bg-[#4AA0FF] focus:outline-none focus:ring-2 focus:ring-[#8BC4FF] sm:w-auto"
              >
                <Download className="size-4" />
                最新版本下载
              </a>
              <a
                href={DOCS_URL}
                className="inline-flex w-full items-center justify-center gap-2 rounded-full border border-white/14 bg-white/[0.04] px-7 py-3.5 text-sm font-semibold text-[#D8E6FF] transition hover:-translate-y-0.5 hover:border-[#2388FF]/60 hover:bg-[#2388FF]/10 focus:outline-none focus:ring-2 focus:ring-[#8BC4FF] sm:w-auto"
              >
                <TerminalSquare className="size-4" />
                查看文档
              </a>
            </motion.div>
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 36, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{ delay: reduceMotion ? 0 : 0.55, duration: 0.65, ease: "easeOut" }}
            className="mt-14 w-full"
          >
            <ProductPreview />
          </motion.div>
        </div>
      </section>

      <section className="relative mx-auto grid max-w-6xl gap-5 px-5 py-16 sm:px-8 lg:grid-cols-3">
        {featureCards.map((feature, index) => {
          const Icon = feature.icon;
          return (
            <motion.article
              key={feature.title}
              initial={{ opacity: 0, y: 34, scale: 0.96 }}
              whileInView={{ opacity: 1, y: 0, scale: 1 }}
              viewport={{ once: true, amount: 0.28 }}
              transition={{
                delay: reduceMotion ? 0 : index * 0.08,
                type: reduceMotion ? "tween" : "spring",
                stiffness: 120,
                damping: 16
              }}
              whileHover={reduceMotion ? undefined : { y: -8 }}
              className="glass-panel group rounded-[26px] p-6 transition hover:border-[#2388FF]/45 hover:shadow-[0_0_40px_rgba(35,136,255,.18)]"
            >
              <div className={clsx("mb-6 flex size-12 items-center justify-center rounded-2xl bg-gradient-to-br", feature.accent)}>
                <Icon className="size-6 text-white" />
              </div>
              <h2 className="text-xl font-semibold text-white">{feature.title}</h2>
              <p className="mt-3 text-sm leading-7 text-[#B5C4DE]">{feature.description}</p>
            </motion.article>
          );
        })}
      </section>

      <DownloadInfo manifest={manifest} />

      <footer className="relative mx-auto max-w-6xl px-5 pb-10 text-sm leading-7 text-[#7E94B8] sm:px-8">
        <div className="border-t border-white/10 pt-6">
          SentryOrbit 是第三方玩家工具，和 CCP Games 无关，不包含游戏作弊、自动操作或破坏客户端公平性的功能。
        </div>
      </footer>
    </main>
  );
}
