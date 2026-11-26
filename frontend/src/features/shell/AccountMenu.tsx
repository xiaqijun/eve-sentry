import { FormEvent, useEffect, useRef, useState } from "react";
import { Avatar, Button, Card, Field, Input, Space, Tag, Typography } from "@/components/ui";
import { ChevronDown, LockKeyhole, LogOut, ShieldCheck, UserRound } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { useOperationFeedback } from "../../components/operationFeedback";
import { changePassword } from "../auth/api";
import { useAuth } from "../auth/AuthContext";

export function AccountMenu() {
  const feedback = useOperationFeedback();
  const { logout, refresh, user } = useAuth();
  const navigate = useNavigate();
  const containerRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  if (!user) return null;

  const updatePassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      feedback.success("密码已更新");
      await refresh().catch((reason) => feedback.operationError(reason, "账号状态刷新失败"));
    } catch (reason) {
      feedback.operationError(reason, "密码更新失败");
    } finally {
      setBusy(false);
    }
  };

  const signOut = async () => {
    setBusy(true);
    try {
      await logout();
      navigate("/login", { replace: true });
    } catch (reason) {
      feedback.operationError(reason, "退出登录失败");
      setBusy(false);
    }
  };

  return (
    <div className="management-account-control" ref={containerRef}>
      <Button
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label="账号设置"
        className="management-topbar-user"
        variant="ghost"
        onClick={() => setOpen((current) => !current)}
      >
        <Avatar size={30}>{(user.display_name || user.username).slice(0, 1).toUpperCase()}</Avatar>
        <span className="management-topbar-user-copy">
          <strong>{user.display_name || user.username}</strong>
          <small>{user.role === "admin" ? "管理员" : "普通用户"}</small>
        </span>
        <ChevronDown className="management-account-chevron" size={14} />
      </Button>

      {open ? (
        <Card aria-label="账号设置" className="management-account-popover" role="dialog">
          <div className="management-account-heading">
            <Avatar size={36}><UserRound size={16} /></Avatar>
            <div>
              <Typography.Text bold>{user.display_name || user.username}</Typography.Text>
              <Typography.Text type="secondary">@{user.username}</Typography.Text>
            </div>
            <Tag color={user.status === "active" ? "green" : "red"}>{user.status === "active" ? "正常" : "已禁用"}</Tag>
          </div>

          <dl className="management-account-details">
            <div><dt>账号角色</dt><dd>{user.role === "admin" ? "管理员" : "普通用户"}</dd></div>
            <div><dt>登录方式</dt><dd>账号密码</dd></div>
          </dl>


          {user.role === "admin" ? (
            <form className="management-account-password" onSubmit={updatePassword}>
              <div className="management-account-section-title"><ShieldCheck size={15} /><strong>修改登录密码</strong></div>
              <Field label="当前密码">
                <Input.Password autoComplete="current-password" required value={currentPassword} onChange={setCurrentPassword} />
              </Field>
              <Field label="新密码">
                <Input.Password autoComplete="new-password" minLength={12} required value={newPassword} onChange={setNewPassword} />
              </Field>
              <Button type="submit" icon={<LockKeyhole size={14} />} loading={busy} long variant="default">更新密码</Button>
            </form>
          ) : null}

          <Space direction="vertical" size={0} style={{ width: "100%" }}>
            <Button className="management-account-logout" icon={<LogOut size={15} />} loading={busy} long status="danger" variant="outline" onClick={() => void signOut()}>退出登录</Button>
          </Space>
        </Card>
      ) : null}
    </div>
  );
}
