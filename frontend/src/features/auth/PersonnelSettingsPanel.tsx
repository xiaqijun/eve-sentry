import { useEffect, useRef, useState } from "react";
import { Alert, Button, Divider, Radio, Space, Switch, Tag, Typography } from "@arco-design/web-react";
import {
  fetchPersonnelSettings, updatePersonnelSettings,
  type PersonnelMode, type PersonnelSettingsSnapshot, type PersonnelValues,
} from "./personnelSettingsApi";

const MODE_LABELS: Record<PersonnelMode, string> = { off: "关闭", shadow: "影子运行", on: "正式启用" };
const MODE_HELP: Record<PersonnelMode, string> = {
  off: "不启动档案任务，使用原解析链路；已有档案和回填进度保留。",
  shadow: "后台建档、回填和刷新，实际识别仍使用原解析链路。",
  on: "识别优先读取档案，资料过期时后台刷新，并更正仍在场人员的名单。",
};

export function PersonnelSettingsPanel() {
  const [snapshot, setSnapshot] = useState<PersonnelSettingsSnapshot | null>(null);
  const [draft, setDraft] = useState<PersonnelValues | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const busy = useRef(false);
  const mounted = useRef(false);

  const load = async () => {
    if (busy.current) return;
    busy.current = true;
    setLoading(true);
    setError("");
    setMessage("");
    try {
      const next = await fetchPersonnelSettings();
      if (mounted.current) { setSnapshot(next); setDraft({ ...next.values }); }
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "配置读取失败，请重试。");
    } finally {
      busy.current = false;
      if (mounted.current) setLoading(false);
    }
  };

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; };
  }, []);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy.current || !snapshot || !draft || !snapshot.writable) return;
    busy.current = true;
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const next = await updatePersonnelSettings(draft, snapshot.revision);
      if (!mounted.current) return;
      setSnapshot(next);
      setDraft({ ...next.values });
      setMessage(next.apply_required || next.restart_required ? "配置已保存但尚未生效，请重新读取并重试应用。" :
        next.effective.mode === "off" ? "配置已保存，档案关闭期间不执行后台任务。" :
          "配置已保存并生效，无需重启；正在执行的任务会安全收尾。");
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "保存失败，请重新读取确认实际配置。");
    } finally {
      busy.current = false;
      if (mounted.current) setSaving(false);
    }
  };

  const change = (values: Partial<PersonnelValues>) => {
    setDraft((current) => current ? { ...current, ...values } : current);
    setMessage("");
  };
  const dirty = !!draft && !!snapshot && JSON.stringify(draft) !== JSON.stringify(snapshot.values);
  const disabled = loading || saving || !snapshot?.writable;
  const pending = !!snapshot && (snapshot.apply_required || snapshot.restart_required);

  return (
    <section aria-label="人员档案配置" className="personnel-settings-panel">
      {error ? <div role="alert"><Alert type="error" content={error} /></div> : null}
      {!snapshot || !draft ? (
        <Space style={{ marginTop: 12 }}>
          <Typography.Text type="secondary">{loading ? "正在读取配置…" : "配置未加载，不能修改。"}</Typography.Text>
          <Button loading={loading} onClick={() => void load()}>重新读取配置</Button>
        </Space>
      ) : (
        <form onSubmit={(event) => void save(event)}>
          <Space wrap style={{ marginBottom: 16 }}>
            <Tag>当前运行：{MODE_LABELS[snapshot.effective.mode]}</Tag>
            <Tag color={pending ? "orange" : "gray"}>已保存：{MODE_LABELS[snapshot.values.mode]}</Tag>
            <Typography.Text type="secondary">来源：{snapshot.source === "database" ? "后台持久配置" : "启动默认值"}</Typography.Text>
          </Space>
          {pending ? <Alert type="warning" content="保存配置与实际运行不一致，请重新读取确认后重试应用；无需重启服务端。" /> : null}
          {!snapshot.available ? <Alert type="warning" content={snapshot.unavailable_reason} /> : null}
          <div className="personnel-settings-grid">
            <div>
              <Typography.Title heading={6}>人员档案模式</Typography.Title>
              <Radio.Group aria-label="人员档案模式" direction="vertical" value={draft.mode}
                disabled={disabled} onChange={(mode: PersonnelMode) => change({ mode })}>
                {Object.entries(MODE_LABELS).map(([mode, label]) => (
                  <Radio key={mode} value={mode} disabled={disabled || (!snapshot.available && mode !== "off")}>{label}</Radio>
                ))}
              </Radio.Group>
              <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>{MODE_HELP[draft.mode]}</Typography.Paragraph>
              <Typography.Paragraph type="secondary">模式保存后在线切换，无需重启或断开监控；推荐先影子验证，再正式启用。</Typography.Paragraph>
            </div>
            <div>
              <Typography.Title heading={6}>后台调度</Typography.Title>
              <Space direction="vertical" size={16} style={{ width: "100%" }}>
                <div>
                  <Space><Switch aria-label="闲时资料刷新" checked={draft.background_refresh} disabled={disabled}
                    onChange={(background_refresh) => change({ background_refresh })} /><Typography.Text>闲时资料刷新</Typography.Text><Typography.Text type="secondary">{draft.background_refresh ? "开启" : "暂停"}</Typography.Text></Space>
                  <Typography.Paragraph type="secondary">暂停非实时资料刷新；当前人员的实时识别、归属刷新和联系人校验仍继续。</Typography.Paragraph>
                </div>
                <div>
                  <Space><Switch aria-label="历史回填" checked={draft.history_backfill} disabled={disabled}
                    onChange={(history_backfill) => change({ history_backfill })} /><Typography.Text>历史回填</Typography.Text><Typography.Text type="secondary">{draft.history_backfill ? "开启" : "暂停"}</Typography.Text></Space>
                  <Typography.Paragraph type="secondary">导入旧缓存及可信历史，暂停不删除进度；与闲时资料刷新独立控制。</Typography.Paragraph>
                </div>
                <div>
                  <Typography.Paragraph>后台并发上限</Typography.Paragraph>
                  <Radio.Group aria-label="后台并发上限" type="button" value={draft.background_max}
                    disabled={disabled || !draft.background_refresh} onChange={(background_max: number) => change({ background_max })}>
                    {[1, 2, 3, 4].map((n) => <Radio key={n} value={n}>{n}</Radio>)}
                  </Radio.Group>
                  <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>设置上限而非固定并发，实际调度会按负载降速，不挤占两个实时识别槽。</Typography.Paragraph>
                </div>
              </Space>
            </div>
          </div>
          <Divider />
          <Space wrap>
            <Button type="primary" htmlType="submit" loading={saving} disabled={disabled || (!dirty && !pending)}>
              {pending && !dirty ? "重试应用" : "保存配置"}
            </Button>
            <Button disabled={loading || saving} onClick={() => { setDraft({ ...snapshot.values }); setMessage(""); setError(""); }}>撤销未保存修改</Button>
            <Button disabled={loading || saving || dirty} loading={loading} onClick={() => void load()}>重新读取配置</Button>
            <Typography.Text type="secondary">{dirty ? "有未保存修改" : "无未保存修改"}</Typography.Text>
          </Space>
          <div role="status" aria-live="polite" style={{ marginTop: 12 }}>
            {saving ? "正在准备并应用配置，监控保持连接…" : message || (snapshot.effective.mode === "off" ? "当前档案已关闭，调度配置保留但不执行。" : "配置保存后在线生效，已执行中的任务安全收尾。")}
          </div>
        </form>
      )}
      <Divider />
    </section>
  );
}
