import { useMemo, useState } from "react";
import { Alert, Button, Form, Message, Modal, Select, Space, Spin, Switch, Typography } from "@arco-design/web-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, fetchMe } from "../auth/api";

interface MapSettings {
  enabled: boolean;
  version: string;
  region_ids: number[];
  system_ids: number[];
  excluded_system_ids: number[];
  regions: Array<{ id: number; name: string }>;
  systems: Array<{ id: number; name: string; region_id: number }>;
}

export function MapSettingsButton() {
  const account = useQuery({ queryKey: ["map-settings-account"], queryFn: fetchMe, retry: false, staleTime: 60000 });
  const client = useQueryClient();
  const [visible, setVisible] = useState(false);
  const [draft, setDraft] = useState<MapSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    setLoading(true); setError(""); setDraft(null);
    try {
      const result = await apiRequest<{ settings: MapSettings }>("/api/v1/admin/map-settings");
      setDraft(result.settings);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "监控范围加载失败");
    } finally { setLoading(false); }
  };
  const count = useMemo(() => {
    if (!draft) return 0;
    const regions = new Set(draft.region_ids), included = new Set(draft.system_ids), excluded = new Set(draft.excluded_system_ids);
    return draft.systems.filter((s) => !excluded.has(s.id) && (regions.has(s.region_id) || included.has(s.id))).length;
  }, [draft]);
  const save = async () => {
    if (!draft || saving || count === 0) return;
    setSaving(true); setError("");
    try {
      const { regions: _regions, systems: _systems, ...payload } = draft;
      await apiRequest("/api/v1/admin/map-settings", { method: "PUT", body: JSON.stringify(payload) });
      await client.invalidateQueries({ queryKey: ["bootstrap"] });
      setVisible(false);
      Message.success("星图监控范围已保存，客户端将自动同步");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，原配置未改变");
    } finally { setSaving(false); }
  };
  if (account.data?.role !== "admin") return null;
  const systemOptions = draft?.systems.map((s) => ({ value: s.id, label: s.name })) || [];
  return <>
    <Button size="small" type="outline" onClick={() => { setVisible(true); void load(); }}>监控范围</Button>
    <Modal title="星图监控范围" visible={visible} style={{ width: "min(720px, calc(100vw - 32px))" }}
      maskClosable={!saving} closable={!saving} onCancel={() => { if (!saving) setVisible(false); }}
      footer={<Space><Button disabled={saving} onClick={() => setVisible(false)}>取消</Button>
        <Button type="primary" loading={saving} disabled={loading || !draft || count === 0} onClick={() => void save()}>保存并应用</Button></Space>}>
      <div style={{ maxHeight: "65vh", overflowY: "auto" }}>
        <Typography.Paragraph type="secondary">选择整个星域，再增补或排除个别星系。区域外客户端仅保留本地检测和预警，不参与远端敌情、节点统计及 QQ 推送。</Typography.Paragraph>
        {error && <Alert type="error" content={<Space direction="vertical"><span>{error}</span><Button disabled={saving} onClick={() => void load()}>重新加载配置</Button></Space>} />}
        {loading && <Spin tip="正在读取星域和星系…" />}
        {draft && <Form layout="vertical">
          <Form.Item label="启用区域限制"><Switch aria-label="启用区域限制" checked={draft.enabled} disabled={saving}
            onChange={(enabled) => setDraft({ ...draft, enabled })} /></Form.Item>
          <Form.Item label="监控星域">
            <Select aria-label="监控星域" mode="multiple" showSearch allowClear maxTagCount={3} disabled={saving}
              value={draft.region_ids} options={draft.regions.map((r) => ({ value: r.id, label: r.name }))}
              onChange={(region_ids: number[]) => setDraft({ ...draft, region_ids })} placeholder="搜索并选择星域" />
          </Form.Item>
          <Form.Item label="额外包含星系">
            <Select aria-label="额外包含星系" mode="multiple" showSearch allowClear maxTagCount={3} disabled={saving}
              value={draft.system_ids} options={systemOptions} onChange={(system_ids: number[]) => setDraft({ ...draft, system_ids })}
              placeholder="加入所选星域以外的星系" />
          </Form.Item>
          <Form.Item label="排除星系">
            <Select aria-label="排除星系" mode="multiple" showSearch allowClear maxTagCount={3} disabled={saving}
              value={draft.excluded_system_ids} options={systemOptions}
              onChange={(excluded_system_ids: number[]) => setDraft({ ...draft, excluded_system_ids })} placeholder="排除优先于包含" />
          </Form.Item>
          <Alert type={count ? "info" : "warning"} content={count
            ? `当前星图覆盖 ${count} 个星系。${draft.enabled ? "范围限制已启用，保存后生效。" : "范围限制未启用，保存只调整地图显示范围。"}`
            : "请至少保留一个星系，空范围不能保存。"} />
        </Form>}
      </div>
    </Modal>
  </>;
}
