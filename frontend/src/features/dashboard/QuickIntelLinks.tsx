import { Tooltip } from "@arco-design/web-react";

export function SystemLink({ name }: { name?: string }) {
  if (!name || name === "未知星系") return <span>未知星系</span>;
  return <a className="console-data-link" href={`/?system=${encodeURIComponent(name)}`} title={`在星图中查看 ${name}`}>{name}</a>;
}

export function PilotLink({ name, id }: { name: string; id?: number }) {
  if (!id || !Number.isSafeInteger(id) || id < 1) return <span>{name}</span>;
  return <a className="console-data-link" href={`https://zkillboard.com/character/${id}/`} target="_blank" rel="noopener noreferrer" aria-label={`${name} · zKill（新窗口）`}>{name}<span aria-hidden="true"> ↗</span></a>;
}

export function SnapshotTime({ value, now }: { value?: string; now: number }) {
  const timestamp = Date.parse(value || "");
  if (!Number.isFinite(timestamp)) return <span>—</span>;
  const seconds = Math.floor((now - timestamp) / 1000);
  const full = new Date(timestamp).toLocaleString("zh-CN", { hour12: false });
  const label = seconds < 0 ? full : seconds < 60 ? "刚刚" : seconds < 3600
    ? `${Math.floor(seconds / 60)} 分钟前` : seconds < 86400
      ? `${Math.floor(seconds / 3600)} 小时前` : `${Math.floor(seconds / 86400)} 天前`;
  return <Tooltip content={full}><time className="console-snapshot-time" dateTime={value} tabIndex={0} aria-label={full}>{label}</time></Tooltip>;
}
