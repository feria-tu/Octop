import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Select,
  Space,
  Spin,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import { enterpriseApi } from "../../../api/modules/enterprise";
import type { OctopAgent } from "../../../context/AgentContext";

interface Target {
  id: number;
  username: string;
}
interface AuditMessage {
  id?: string;
  role?: string;
  type?: string;
  content?: unknown;
}

function messageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content
      .map((part) => (typeof part === "string" ? part : part?.text ?? ""))
      .join("\n");
  return "";
}

export default function EmployeeAuditDrawer({
  target,
  agents,
  onClose,
}: {
  target: Target | null;
  agents: OctopAgent[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [agentId, setAgentId] = useState<string>();
  const [threads, setThreads] = useState<
    { thread_id: string; title: string | null }[]
  >([]);
  const [threadId, setThreadId] = useState<string>();
  const [messages, setMessages] = useState<AuditMessage[]>([]);
  const [artifacts, setArtifacts] = useState<string[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>();
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  useEffect(() => {
    setAgentId(undefined);
    setThreadId(undefined);
    setArtifacts([]);
    setHasMore(false);
    setThreads([]);
    setMessages([]);
    setError(undefined);
  }, [target?.id]);
  useEffect(() => {
    let active = true;
    setThreadId(undefined);
    setMessages([]);
    setThreads([]);
    setError(undefined);
    if (!target || !agentId) return;
    setLoading(true);
    enterpriseApi
      .auditThreads(agentId, target.id)
      .then((rows) => {
        if (active) setThreads(rows);
      })
      .catch((err) => {
        if (active) setError(String(err.message ?? err));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [target, agentId]);
  useEffect(() => {
    let active = true;
    if (!target || !agentId || !threadId) return;
    setLoading(true);
    setError(undefined);
    setArtifacts([]);
    setHasMore(false);
    enterpriseApi
      .auditHistory(agentId, target.id, threadId)
      .then((history) => {
        if (active) {
          setMessages(history.messages);
          setArtifacts(history.artifacts ?? []);
          setHasMore(history.has_more);
          setCursor(history.next_cursor);
          setOffset(history.messages.length);
        }
      })
      .catch((err) => {
        if (active) setError(String(err.message ?? err));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [target, agentId, threadId]);
  const available = agents.filter(
    (agent) =>
      agent.enterprise_kind === "department" ||
      (agent.enterprise_kind === "personal" && agent.user_id === target?.id),
  );
  return (
    <Drawer
      open={!!target}
      title={`${t("enterprise.audit")} · ${target?.username ?? ""}`}
      width={720}
      onClose={onClose}
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Alert type="info" message={t("enterprise.auditHint")} />
        <Select
          style={{ width: "100%" }}
          placeholder={t("enterprise.selectAssistant")}
          value={agentId}
          onChange={setAgentId}
          options={available.map((agent) => ({
            value: agent.agent_id,
            label: agent.name,
          }))}
        />
        <Select
          style={{ width: "100%" }}
          placeholder={t("enterprise.selectConversation")}
          value={threadId}
          onChange={setThreadId}
          options={threads.map((thread) => ({
            value: thread.thread_id,
            label: thread.title || thread.thread_id,
          }))}
        />
        {error && <Alert type="error" message={error} />}
        {loading ? (
          <Spin />
        ) : messages.length ? (
          messages.map((msg, index) => (
            <div key={msg.id ?? index}>
              <Typography.Text type="secondary">
                {msg.role ?? msg.type}
              </Typography.Text>
              <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
                {messageText(msg.content)}
              </Typography.Paragraph>
            </div>
          ))
        ) : (
          <Empty description={t("enterprise.emptyAudit")} />
        )}
        {hasMore && (
          <Button
            loading={loading}
            onClick={async () => {
              if (!target || !agentId || !threadId) return;
              setLoading(true);
              try {
                const history = await enterpriseApi.auditHistory(
                  agentId,
                  target.id,
                  threadId,
                  offset,
                  cursor,
                );
                setMessages((current) => [...history.messages, ...current]);
                setOffset((current) => current + history.messages.length);
                setCursor(history.next_cursor);
                setHasMore(history.has_more);
              } catch (err) {
                setError(err instanceof Error ? err.message : String(err));
              } finally {
                setLoading(false);
              }
            }}
          >
            {t("enterprise.moreHistory")}
          </Button>
        )}
        {artifacts.map((path) => (
          <Button
            key={path}
            onClick={async () => {
              if (!target || !agentId) return;
              try {
                const blob = await enterpriseApi.auditFile(
                  agentId,
                  target.id,
                  path,
                );
                const url = URL.createObjectURL(blob);
                const link = document.createElement("a");
                link.href = url;
                link.download =
                  path.replaceAll("\\", "/").split("/").pop() ?? "file";
                link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
              } catch (err) {
                setError(err instanceof Error ? err.message : String(err));
              }
            }}
          >
            {path.replaceAll("\\", "/").split("/").pop()}
          </Button>
        ))}
        <Button onClick={onClose}>{t("common.close")}</Button>
      </Space>
    </Drawer>
  );
}
