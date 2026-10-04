import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Descriptions, Space, Tag, Typography } from "@arco-design/web-react";
import { fetchEsiLoginStatus, fetchEsiStatus, startEsiLogin } from "./api";
import type { EsiAuthStatus, EsiLoginSnapshot } from "./types";

function formatExpiry(value?: number): string {
  if (!value) return "未知";
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? "未知" : date.toLocaleString("zh-CN", { hour12: false });
}

export function EsiAuthPanel() {
  const [status, setStatus] = useState<EsiAuthStatus | null>(null);
  const [login, setLogin] = useState<EsiLoginSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);

  const load = useCallback(async () => {
    try {
      const next = await fetchEsiStatus();
      if (mounted.current) { setStatus(next); setError(""); }
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "ESI 授权状态读取失败");
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; };
  }, [load]);

  useEffect(() => {
    if (login?.status !== "pending") return undefined;
    const timer = window.setInterval(async () => {
      try {
        const next = await fetchEsiLoginStatus();
        if (!mounted.current) return;
        setLogin(next);
        if (next.status === "authenticated") await load();
      } catch (reason) {
        if (mounted.current) setError(reason instanceof Error ? reason.message : "ESI 登录状态读取失败");
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [login?.status, load]);

  const beginLogin = async () => {
    setStarting(true);
    setError("");
    try {
      const next = await startEsiLogin();
      if (!mounted.current) return;
      setLogin(next);
      if (next.authorization_url) window.open(next.authorization_url, "_blank", "noopener,noreferrer");
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "无法启动 ESI 登录");
    } finally {
      if (mounted.current) setStarting(false);
    }
  };

  const authenticated = !!status?.authenticated;
  const disabled = loading || starting || !status?.config?.client_id_configured;

  return (
    <Card className="arco-management-card" title="EVE ESI 授权（组织声望）">
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        {error ? <Alert type="error" content={error} /> : null}
        {status && !status.config?.client_id_configured ? (
          <Alert type="warning" content="服务端尚未配置 EVE OAuth2 Client ID；配置后才能读取组织联系人声望。" />
        ) : null}
        <Space wrap>
          <Tag color={authenticated ? "green" : "orange"}>
            {authenticated ? "已连接" : login?.status === "pending" ? "等待 EVE 授权" : "未连接"}
          </Tag>
          {authenticated && status?.character_id ? <Typography.Text>角色 ID：{status.character_id}</Typography.Text> : null}
          {status?.expired ? <Tag color="orange">访问令牌已过期，将使用刷新令牌续期</Tag> : null}
        </Space>
        <Descriptions size="small" column={{ xs: 1, sm: 2 }} data={[
          { label: "组织声望读取", value: authenticated ? "服务端已启用" : "等待授权" },
          { label: "令牌存储", value: status?.config?.token_storage || "服务端配置" },
          { label: "令牌到期", value: formatExpiry(status?.expires_at) },
          { label: "回调地址", value: status?.config?.redirect_uri || "未配置" },
        ]} />
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          授权只用于服务端读取联系人关系并判断敌我；访问令牌和刷新令牌不会返回浏览器，也不会写入客户端。
        </Typography.Paragraph>
        <Button type="primary" loading={starting} disabled={disabled} onClick={() => void beginLogin()}>
          {authenticated ? "重新授权 EVE 账号" : "连接 EVE 账号"}
        </Button>
      </Space>
    </Card>
  );
}
