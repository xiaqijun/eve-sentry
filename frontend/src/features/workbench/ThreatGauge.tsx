import ReactECharts from "echarts-for-react";

interface ThreatGaugeProps {
  alerts: number;
  hostiles: number;
  observations: number;
  score: number;
  title: string;
}

export function ThreatGauge({
  alerts,
  hostiles,
  observations,
  score,
  title,
}: ThreatGaugeProps) {
  const option = {
    backgroundColor: "transparent",
    series: [
      {
        axisLabel: { show: false },
        axisLine: {
          lineStyle: {
            color: [
              [0.35, "#1fd2ff"],
              [0.7, "#ffb21a"],
              [1, "#ff4038"],
            ],
            width: 14,
          },
        },
        axisTick: { show: false },
        detail: {
          color: "#ff4d42",
          fontSize: 32,
          fontWeight: 800,
          formatter: "{value}",
          offsetCenter: [0, "2%"],
        },
        max: 100,
        min: 0,
        pointer: { show: false },
        progress: {
          itemStyle: {
            color: "#ff4038",
          },
          show: true,
          width: 14,
        },
        radius: "92%",
        splitLine: { show: false },
        title: {
          color: "#9fb7c4",
          fontSize: 12,
          offsetCenter: [0, "38%"],
        },
        type: "gauge",
        data: [{ name: title, value: Math.min(Math.max(score, 0), 100) }],
      },
    ],
  };

  return (
    <div className="threat-gauge" data-testid="threat-gauge">
      <ReactECharts option={option} style={{ height: 150, width: 150 }} />
      <div className="gauge-factors">
        <span>预警事件 <strong>{alerts}</strong></span>
        <span>观察记录 <strong>{observations}</strong></span>
        <span>敌对目标 <strong>{hostiles}</strong></span>
      </div>
    </div>
  );
}
