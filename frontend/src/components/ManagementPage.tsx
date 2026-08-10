import { Alert, Avatar, Button, Card, Grid, Statistic, Tag, Typography } from "@arco-design/web-react";
import { IconRefresh } from "@arco-design/web-react/icon";

export function ManagementPageHeader({
  title,
  subtitle,
  kicker,
  loading = false,
  onRefresh,
  refreshLabel = "刷新",
  extra,
}: {
  title: string;
  subtitle?: string;
  kicker?: string;
  loading?: boolean;
  onRefresh?: () => void;
  refreshLabel?: string;
  extra?: React.ReactNode;
}) {
  return (
    <header className="content-page-header account-header arco-page-header">
      <div className="management-page-heading">
        {kicker ? <Typography.Text className="content-page-kicker">{kicker}</Typography.Text> : null}
        <Typography.Title heading={4}>{title}</Typography.Title>
        {subtitle ? <Typography.Text className="management-page-subtitle" type="secondary">{subtitle}</Typography.Text> : null}
      </div>
      <div aria-label="页面操作" className="arco-page-header-actions" role="toolbar">
        {extra}
        {onRefresh ? (
          <Button icon={<IconRefresh />} loading={loading} type="outline" onClick={onRefresh}>
            {refreshLabel}
          </Button>
        ) : null}
      </div>
    </header>
  );
}

export function ManagementError({ error }: { error: string }) {
  return error ? (
    <div aria-live="assertive" className="arco-page-alert" role="alert">
      <Alert closable={false} content={error} type="error" />
    </div>
  ) : null;
}

export function ManagementSummary({
  ariaLabel,
  items,
}: {
  ariaLabel: string;
  items: Array<{ label: string; value: number | string }>;
}) {
  const span = items.length === 3 ? 8 : 6;
  return (
    <section aria-label={ariaLabel} className="arco-summary-grid">
      <Grid.Row gutter={16}>
        {items.map((item) => (
          <Grid.Col key={item.label} md={span} sm={12} xs={24}>
            <Card className="arco-summary-card">
              <Statistic title={item.label} value={item.value} />
            </Card>
          </Grid.Col>
        ))}
      </Grid.Row>
    </section>
  );
}

export function AccountStatusTag({ status }: { status: "active" | "disabled" }) {
  return <Tag color={status === "active" ? "green" : "red"}>{status === "active" ? "正常" : "已禁用"}</Tag>;
}

export function KeyStatusTag({ status }: { status: "active" | "revoked" }) {
  return <Tag color={status === "active" ? "green" : "red"}>{status === "active" ? "有效" : "已吊销"}</Tag>;
}

export function UserIdentity({ displayName, username }: { displayName: string; username: string }) {
  const name = displayName || username;
  return (
    <div className="arco-user-identity management-user-cell">
      <Avatar size={32}>{name.slice(0, 1).toUpperCase()}</Avatar>
      <span><strong>{name}</strong><small>@{username}</small></span>
    </div>
  );
}
