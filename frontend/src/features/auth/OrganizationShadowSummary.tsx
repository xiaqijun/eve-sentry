import { Typography } from "@arco-design/web-react";

export function OrganizationShadowSummary({ counts }: { counts?: Record<string, number> }) {
  if (counts?.comparable === undefined) {
    return <Typography.Paragraph type="secondary">
      旧版统计：已比较 {counts?.compared ?? 0} 次，差异 {counts?.different ?? 0} 次。
      含待确认结果，不能作为敌我差异率；请等待新版服务端统计。
    </Typography.Paragraph>;
  }
  const comparable = counts.comparable;
  const different = counts.decision_different ?? 0;
  return <Typography.Paragraph type="secondary">
    总评估 {counts.compared ?? 0} 次；有效比较 {comparable} 次；待确认 {counts.pending ?? 0} 次；
    敌我差异 {different} 次；差异率 {comparable > 0 ? `${(100 * different / comparable).toFixed(1)}%` : "暂无（无有效样本）"}。
    仅双方均有确定结果时计入差异率；按调用次数统计，非去重人数，服务重启后重新累计。
  </Typography.Paragraph>;
}
