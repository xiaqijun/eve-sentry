import type { Metadata, Viewport } from "next";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/orbitron/500.css";
import "@fontsource/orbitron/600.css";
import "@fontsource/orbitron/700.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "SentryOrbit | EVE Online Warning Client Download",
  description:
    "SentryOrbit is a third-party EVE Online warning tool download page for the Windows client, release metadata, and checksum details.",
  applicationName: "SentryOrbit",
  keywords: ["SentryOrbit", "EVE Online", "warning tool", "Windows client", "download"],
  authors: [{ name: "SentryOrbit" }],
  robots: {
    index: true,
    follow: true
  }
};

export const viewport: Viewport = {
  themeColor: "#050816",
  colorScheme: "dark"
};

export default function RootLayout({
  children
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
