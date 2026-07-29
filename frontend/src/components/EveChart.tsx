import ReactEChartsCore from "echarts-for-react/lib/core";
import * as echarts from "echarts/core";
import { LineChart, PieChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";

echarts.use([
  GridComponent,
  LegendComponent,
  LineChart,
  PieChart,
  SVGRenderer,
  TooltipComponent,
]);

interface EveChartProps {
  className?: string;
  height?: number;
  option: EChartsOption;
}

export function EveChart({ className, height = 280, option }: EveChartProps) {
  return (
    <ReactEChartsCore
      echarts={echarts}
      className={className}
      lazyUpdate
      notMerge
      option={option}
      opts={{ renderer: "svg" }}
      style={{ height, width: "100%" }}
    />
  );
}
