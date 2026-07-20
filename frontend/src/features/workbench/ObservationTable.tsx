import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";

import type { PilotObservation } from "./types";

interface ObservationTableProps {
  observations: PilotObservation[];
}

function formatClock(value?: string): string {
  if (!value) {
    return "--:--";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    hour12: false,
    minute: "2-digit",
  });
}

function levelLabel(level?: string): string {
  switch (level) {
    case "critical":
      return "严重";
    case "high":
      return "高危";
    case "medium":
      return "中危";
    case "low":
      return "低危";
    default:
      return "未知";
  }
}

const columnHelper = createColumnHelper<PilotObservation>();

const columns = [
  columnHelper.accessor("pilotName", {
    cell: (info) => (
      <strong className="pilot-name" title={info.getValue()}>
        {info.getValue()}
      </strong>
    ),
    header: "飞行员",
  }),
  columnHelper.accessor((row) => row.systemName || "未知", {
    cell: (info) => info.getValue(),
    header: "星系",
    id: "systemName",
  }),
  columnHelper.accessor((row) => row.sources.join(" / ") || "情报", {
    cell: (info) => info.getValue(),
    header: "来源",
    id: "sources",
  }),
  columnHelper.accessor("level", {
    cell: (info) => <em>{levelLabel(info.getValue())}</em>,
    header: "威胁",
  }),
  columnHelper.accessor((row) => formatClock(row.latestSeen), {
    cell: (info) => info.getValue(),
    header: "最近出现",
    id: "latestSeen",
  }),
  columnHelper.accessor((row) => row.repeatCount ?? row.evidenceCount, {
    cell: (info) => info.getValue(),
    header: "次数",
    id: "count",
  }),
];

export function ObservationTable({ observations }: ObservationTableProps) {
  const table = useReactTable({
    columns,
    data: observations,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <div className="observation-table" data-testid="observation-table">
      <table>
        <thead>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <th key={header.id}>
                  {header.isPlaceholder
                    ? null
                    : flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr className={`level-${row.original.level}`} key={row.id}>
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id}>
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {observations.length === 0 ? (
        <div className="empty-state">暂无实时敌对目标</div>
      ) : null}
      <div className="source-strip">
        {observations.slice(0, 6).map((item) => (
          <div className="source-tags" key={item.id}>
            <span>{item.pilotName}</span>
            {item.sources.map((source) => (
              <i key={source}>{source}</i>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
