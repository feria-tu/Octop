import { useState } from "react";
import {
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useAgent, type OctopAgent } from "../../../context/AgentContext";
import { enterpriseApi } from "../../../api/modules/enterprise";
import { message } from "../../../utils/antdMessage";

export default function ManagedAssistantsPanel() {
  const { agents, setActiveAgent, refresh } = useAgent();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [editing, setEditing] = useState<OctopAgent | null>(null);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<{
    name: string;
    icon_name: string | null;
    description: string | null;
    system_prompt: string | null;
  }>();
  const managed = agents.filter(
    (a) =>
      a.enterprise_kind === "personal" || a.enterprise_kind === "department",
  );
  if (!managed.length) return null;
  return (
    <Space direction="vertical" style={{ width: "100%", padding: "16px 24px" }}>
      {(["personal", "department"] as const).map((kind) => (
        <Card
          key={kind}
          title={t(
            kind === "personal"
              ? "enterprise.myAssistant"
              : "enterprise.departmentAgents",
          )}
        >
          <Space direction="vertical" style={{ width: "100%" }}>
            {managed
              .filter((a) => a.enterprise_kind === kind)
              .map((agent) => (
                <Space
                  key={agent.agent_id}
                  style={{ width: "100%", justifyContent: "space-between" }}
                >
                  <div>
                    <Typography.Text strong>{agent.name}</Typography.Text>
                    <Typography.Paragraph type="secondary">
                      {agent.description}
                    </Typography.Paragraph>
                  </div>
                  <Space>
                    <Button
                      onClick={() => {
                        setActiveAgent(agent.agent_id);
                        navigate("/chat");
                      }}
                    >
                      {t("enterprise.chat")}
                    </Button>
                    {kind === "personal" &&
                      agent.allowed_actions?.edit_profile && (
                        <Button
                          onClick={() => {
                            form.setFieldsValue({
                              name: agent.name,
                              icon_name: agent.icon_name ?? null,
                              description: agent.description,
                              system_prompt: agent.system_prompt ?? null,
                            });
                            setEditing(agent);
                          }}
                        >
                          {t("common.edit")}
                        </Button>
                      )}
                  </Space>
                </Space>
              ))}
          </Space>
        </Card>
      ))}
      <Modal
        title={t("enterprise.myAssistant")}
        open={editing !== null}
        confirmLoading={saving}
        onCancel={() => setEditing(null)}
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values) => {
            if (!editing) return;
            setSaving(true);
            try {
              await enterpriseApi.updateProfile(editing.agent_id, values);
              await refresh();
              setEditing(null);
            } catch (err) {
              message.error(
                err instanceof Error ? err.message : t("common.saveFailed"),
              );
            } finally {
              setSaving(false);
            }
          }}
        >
          <Form.Item
            name="name"
            label={t("enterprise.name")}
            rules={[{ required: true, whitespace: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="icon_name" label={t("enterprise.avatar")}>
            <Select
              allowClear
              options={["cpu", "user", "book-open", "sparkles"].map(
                (value, index) => ({
                  value,
                  label: ["🤖", "👤", "📚", "✨"][index],
                }),
              )}
            />
          </Form.Item>
          <Form.Item name="description" label={t("enterprise.description")}>
            <Input.TextArea />
          </Form.Item>
          <Form.Item name="system_prompt" label={t("enterprise.persona")}>
            <Input.TextArea rows={6} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
