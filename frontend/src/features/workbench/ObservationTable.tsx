import { Table } from "@arco-design/web-react";

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

const columns = [
  { title: "飞行员", dataIndex: "pilotName", render: (value: string) => <strong className="pilot-name" title={value}>{value}</strong> },
  { title: "星系", dataIndex: "systemName", render: (value?: string) => value || "未知" },
  { title: "来源", dataIndex: "sources", render: (value: string[]) => value.join(" / ") || "情报" },
  { title: "最近出现", dataIndex: "latestSeen", render: (value?: string) => formatClock(value) },
  { title: "次数", render: (_: unknown, row: PilotObservation) => row.repeatCount ?? row.evidenceCount },
];

export function ObservationTable({ observations }: ObservationTableProps) {
  return (
    <div className="observation-table" data-testid="observation-table">
      <Table<PilotObservation>
        border={false}
        columns={columns}
        data={observations}
        noDataElement={<div className="empty-state">暂无实时敌对目标</div>}
        pagination={false}
        rowClassName={(record) => `level-${record.level}`}
        rowKey="id"
        size="mini"
      />
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
