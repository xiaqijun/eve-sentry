import { ChevronLeft, ChevronRight } from "lucide-react";
import { useState, type Key, type ReactNode } from "react";
import { cn } from "../../lib/utils";
import { Button } from "./button";
import { Empty, Spin } from "./presentation";

export type TableColumnProps<Row> = {
  title: ReactNode;
  dataIndex?: string;
  width?: number | string;
  render?(value: Row[keyof Row] | undefined, row: Row, index: number): ReactNode;
};

export function Table<Row extends object>({ columns, data, rowKey, rowClassName, scroll, pagination, noDataElement, loading, className, size }:
  { columns: TableColumnProps<Row>[]; data: Row[]; rowKey: string | ((row: Row) => Key);
    rowClassName?: (row: Row) => string; scroll?: { x?: number | string; y?: number | string };
    pagination?: false | { pageSize: number; size?: "mini" }; noDataElement?: ReactNode;
    loading?: boolean; className?: string; size?: "small" | "mini"; border?: false; }) {
  const [page, setPage] = useState(1);
  const pageSize = pagination ? pagination.pageSize : data.length || 1;
  const currentPage = Math.min(page, Math.max(1, Math.ceil(data.length / pageSize)));
  const rows = data.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  return <div className={cn("ui-table", className)} data-size={size} aria-busy={loading || undefined}>
    <div className="ui-table-container" style={{ maxHeight: scroll?.y }}>
      <table style={{ minWidth: scroll?.x }}>
        <thead><tr>{columns.map((column, index) => <th key={column.dataIndex ?? index} scope="col"
          className="ui-table-th" style={{ width: column.width }}>{column.title}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => {
            const values = row as Record<string, Row[keyof Row]>;
            const key = typeof rowKey === "function" ? rowKey(row) : String(values[rowKey]);
            return <tr className={cn("ui-table-tr", rowClassName?.(row))} key={key}>
              {columns.map((column, columnIndex) => {
                const value = column.dataIndex == null ? undefined : values[column.dataIndex];
                return <td className="ui-table-td" key={column.dataIndex ?? columnIndex}>
                  {column.render ? column.render(value, row, index) : value == null ? "—" : String(value)}
                </td>;
              })}
            </tr>;
          })}
          {rows.length === 0 ? <tr><td colSpan={columns.length} className="ui-table-td">
            {loading ? <Spin /> : <Empty description={noDataElement} />}
          </td></tr> : null}
        </tbody>
      </table>
    </div>
    {pagination && data.length > pagination.pageSize ?
      <Pagination current={currentPage} pageSize={pageSize} total={data.length} onChange={setPage} /> : null}
  </div>;
}

export function Pagination({ current, pageSize, total, onChange, showTotal, className }:
  { current: number; pageSize: number; total: number; onChange: (page: number) => void; showTotal?: boolean; className?: string; }) {
  const last = Math.max(1, Math.ceil(total / pageSize));
  const pages = [...new Set([1, current - 1, current, current + 1, last].filter((page) => page >= 1 && page <= last))].sort((a, b) => a - b);
  return <nav className={cn("ui-pagination", className)} aria-label="分页">
    {showTotal ? <span className="text-sm text-muted-foreground">共 {total} 条</span> : null}
    <Button variant="ghost" size="xs" icon={<ChevronLeft size={16} />} aria-label="上一页" disabled={current <= 1} onClick={() => onChange(current - 1)} />
    {pages.map((page, index) => <span key={page} className="inline-flex items-center">
      {index > 0 && page - pages[index - 1] > 1 ? <span aria-hidden="true">…</span> : null}
      <Button className={cn("ui-pagination-item", current === page && "ui-pagination-item-active")}
        variant={page === current ? "default" : "ghost"} size="xs" aria-current={page === current ? "page" : undefined}
        aria-label={`第 ${page} 页`} onClick={() => onChange(page)}>{page}</Button>
    </span>)}
    <Button variant="ghost" size="xs" icon={<ChevronRight size={16} />} aria-label="下一页" disabled={current >= last} onClick={() => onChange(current + 1)} />
  </nav>;
}

export function Descriptions({ data, column = 1, border, className }:
  { data: Array<{ label: ReactNode; value: ReactNode }>; column?: number | { xs?: number; sm?: number; lg?: number };
    border?: boolean; size?: "small"; className?: string; }) {
  const xs = typeof column === "number" ? column : column.xs ?? 1;
  const sm = typeof column === "number" ? column : column.sm ?? xs;
  const lg = typeof column === "number" ? column : column.lg ?? sm;
  return <dl className={cn("ui-descriptions", border && "ui-descriptions-bordered", className)}
    style={{ "--desc-xs": xs, "--desc-sm": sm, "--desc-lg": lg } as React.CSSProperties}>
    {data.map((item, index) => <div key={index} className="ui-descriptions-item">
      <dt className="ui-descriptions-item-label">{item.label}</dt>
      <dd className="ui-descriptions-item-value">{item.value ?? "—"}</dd>
    </div>)}
  </dl>;
}
