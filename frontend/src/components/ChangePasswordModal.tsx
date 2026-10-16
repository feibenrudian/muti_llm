/** 改密弹窗（管理台/门户共用，T52 抽取）：旧密码 + 新密码，成功后其他会话失效。 */

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError } from "../lib/api";
import { Button, ErrorText, Field, Input, Modal } from "./ui";

export default function ChangePasswordModal({
  open,
  onClose,
  change,
}: {
  open: boolean;
  onClose: () => void;
  /** 会话作用域对应的改密 API（api.auth.changePassword / api.portal.auth.changePassword）。 */
  change: (oldPassword: string, newPassword: string) => Promise<{ ok: boolean }>;
}) {
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const changeMutation = useMutation({
    mutationFn: () => change(oldPassword, newPassword),
    onSuccess: () => {
      setDone(true);
      setOldPassword("");
      setNewPassword("");
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const close = () => {
    setDone(false);
    setError("");
    onClose();
  };

  return (
    <Modal open={open} title="修改密码" onClose={close}>
      <div className="space-y-3">
        {done ? (
          <p className="text-sm text-emerald-600">密码已修改，其他会话已退出。</p>
        ) : (
          <>
            <ErrorText>{error}</ErrorText>
            <Field label="当前密码">
              <Input
                type="password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
              />
            </Field>
            <Field label="新密码" hint="至少 8 位；修改后其他登录会话将失效">
              <Input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
              />
            </Field>
            <div className="flex justify-end">
              <Button
                disabled={!oldPassword || newPassword.length < 8 || changeMutation.isPending}
                onClick={() => changeMutation.mutate()}
              >
                修改
              </Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
