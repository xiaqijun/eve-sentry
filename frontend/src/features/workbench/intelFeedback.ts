/** Shared wording for data availability; no inference of safety from an empty list. */
export function intelFeedback(hasData: boolean, failed: boolean, onlineSystems: number): string {
  if (failed) return hasData
    ? "数据更新异常，当前显示上次数据，不代表最新态势"
    : "实时态势数据加载失败，请刷新后重试";
  if (!hasData) return "正在获取监控与敌情数据…";
  return onlineSystems > 0
    ? "当前监控范围内没有敌对"
    : "暂无在线监控，暂不能判断星系是否安全";
}
