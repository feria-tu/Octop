import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  enterpriseApi,
  type Department,
  type DepartmentAgent,
  type PersonalDefaults,
} from "../../../api/modules/enterprise";
import { providerApi } from "../../../api/modules/provider";
import { knowledgeBasesApi } from "../../../api/modules/knowledgeBases";
import { buildModelSelectOptions } from "../../../utils/modelOptions";
import { message } from "../../../utils/antdMessage";

interface AgentValues extends PersonalDefaults {
  department_id: string;
  description: string | null;
  knowledge_base_ids: string[];
}

export default function DepartmentsPanel() {
  const { t } = useTranslation();
  const [departments, setDepartments] = useState<Department[]>([]);
  const [agents, setAgents] = useState<DepartmentAgent[]>([]);
  const [modelOptions, setModelOptions] = useState<
    { label: string; value: string }[]
  >([]);
  const [knowledgeOptions, setKnowledgeOptions] = useState<
    { label: string; value: string }[]
  >([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingDepartment, setEditingDepartment] = useState<
    Department | "new" | null
  >(null);
  const [departmentName, setDepartmentName] = useState("");
  const [editingAgent, setEditingAgent] = useState<
    DepartmentAgent | "new" | null
  >(null);
  const [saving, setSaving] = useState(false);
  const [defaultsForm] = Form.useForm<PersonalDefaults>();
  const [agentForm] = Form.useForm<AgentValues>();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [ds, as, defaults, models, knowledge] = await Promise.all([
        enterpriseApi.departments(),
        enterpriseApi.agents(),
        enterpriseApi.defaults(),
        providerApi.listResolvedModels(),
        knowledgeBasesApi.list(),
      ]);
      setModelOptions(
        buildModelSelectOptions(models, t("enterprise.autoModel")),
      );
      setKnowledgeOptions(
        knowledge.map((kb) => ({ label: kb.name, value: kb.id })),
      );
      setDepartments(ds);
      setAgents(as);
      defaultsForm.setFieldsValue(defaults);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("enterprise.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [defaultsForm, t]);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const mutate = async (operation: () => Promise<unknown>) => {
    setSaving(true);
    try {
      await operation();
      await refresh();
      message.success(t("common.saveSuccess"));
      return true;
    } catch (err) {
      message.error(
        err instanceof Error ? err.message : t("common.saveFailed"),
      );
      return false;
    } finally {
      setSaving(false);
    }
  };

  return (
    <Space
      direction="vertical"
      size="large"
      style={{ width: "100%", padding: 24 }}
    >
      {error && <Alert type="error" message={error} showIcon />}
      <Card
        title={t("enterprise.departments")}
        extra={
          <Button
            onClick={() => {
              setDepartmentName("");
              setEditingDepartment("new");
            }}
          >
            {t("enterprise.newDepartment")}
          </Button>
        }
      >
        <Typography.Paragraph type="secondary">
          {t("enterprise.departmentHint")}
        </Typography.Paragraph>
        <Table
          rowKey="department_id"
          dataSource={departments}
          loading={loading}
          pagination={false}
          columns={[
            { title: t("enterprise.name"), dataIndex: "name" },
            {
              title: t("common.actions"),
              render: (_, row: Department) => (
                <Space>
                  <Button
                    onClick={() => {
                      setDepartmentName(row.name);
                      setEditingDepartment(row);
                    }}
                  >
                    {t("common.edit")}
                  </Button>
                  <Popconfirm
                    title={t("enterprise.deleteDepartmentHint")}
                    onConfirm={() =>
                      mutate(() =>
                        enterpriseApi.deleteDepartment(row.department_id),
                      )
                    }
                  >
                    <Button danger>{t("common.delete")}</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Card title={t("enterprise.personalDefaults")}>
        <Typography.Paragraph type="secondary">
          {t("enterprise.defaultsHint")}
        </Typography.Paragraph>
        <Form
          form={defaultsForm}
          layout="vertical"
          onFinish={(values) =>
            void mutate(() =>
              enterpriseApi.saveDefaults({
                ...values,
                default_model: values.default_model?.trim() || null,
                system_prompt: values.system_prompt || null,
              }),
            )
          }
        >
          <Form.Item name="name" label={t("enterprise.name")}>
            <Input
              placeholder={t("enterprise.defaultNameHint")}
              maxLength={100}
            />
          </Form.Item>
          <Form.Item name="default_model" label={t("enterprise.model")}>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              options={modelOptions}
            />
          </Form.Item>
          <Form.Item name="system_prompt" label={t("enterprise.persona")}>
            <Input.TextArea rows={4} />
          </Form.Item>
          <Button htmlType="submit" loading={saving}>
            {t("common.save")}
          </Button>
        </Form>
      </Card>
      <Card
        title={t("enterprise.departmentAgents")}
        extra={
          <Button
            disabled={!departments.length}
            onClick={() => {
              agentForm.resetFields();
              setEditingAgent("new");
            }}
          >
            {t("enterprise.newAgent")}
          </Button>
        }
      >
        <Typography.Paragraph type="secondary">
          {t("enterprise.agentHint")}
        </Typography.Paragraph>
        <Table
          rowKey="agent_id"
          dataSource={agents}
          loading={loading}
          columns={[
            { title: t("enterprise.name"), dataIndex: "name" },
            {
              title: t("enterprise.department"),
              render: (_, row: DepartmentAgent) =>
                departments.find((d) => d.department_id === row.department_id)
                  ?.name ?? "—",
            },
            {
              title: t("enterprise.published"),
              render: (_, row: DepartmentAgent) => (
                <Switch
                  checked={row.enabled}
                  loading={saving}
                  onChange={(enabled) =>
                    void mutate(() =>
                      enterpriseApi.updateAgent(row.agent_id, { enabled }),
                    )
                  }
                />
              ),
            },
            {
              title: t("common.actions"),
              render: (_, row: DepartmentAgent) => (
                <Space>
                  <Button
                    onClick={() => {
                      agentForm.setFieldsValue(row);
                      setEditingAgent(row);
                    }}
                  >
                    {t("common.edit")}
                  </Button>
                  <Popconfirm
                    title={t("enterprise.deleteAgentHint")}
                    onConfirm={() =>
                      mutate(() => enterpriseApi.deleteAgent(row.agent_id))
                    }
                  >
                    <Button danger>{t("common.delete")}</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Modal
        title={t("enterprise.department")}
        open={editingDepartment !== null}
        confirmLoading={saving}
        onCancel={() => setEditingDepartment(null)}
        onOk={async () => {
          const name = departmentName.trim();
          if (!name) return;
          if (
            await mutate(() =>
              editingDepartment === "new"
                ? enterpriseApi.createDepartment(name)
                : enterpriseApi.renameDepartment(
                    (editingDepartment as Department).department_id,
                    name,
                  ),
            )
          )
            setEditingDepartment(null);
        }}
      >
        <Input
          value={departmentName}
          maxLength={100}
          onChange={(event) => setDepartmentName(event.target.value)}
        />
      </Modal>
      <Drawer
        title={t("enterprise.departmentAgents")}
        open={editingAgent !== null}
        onClose={() => setEditingAgent(null)}
        width={520}
      >
        <Form
          form={agentForm}
          layout="vertical"
          onFinish={async (values) => {
            const { department_id, ...configuration } = values;
            const payload = {
              ...configuration,
              description: configuration.description || null,
              default_model: configuration.default_model?.trim() || null,
              system_prompt: configuration.system_prompt || null,
              knowledge_base_ids: configuration.knowledge_base_ids || [],
            };
            if (
              await mutate(() =>
                editingAgent === "new"
                  ? enterpriseApi.createAgent(department_id, payload)
                  : enterpriseApi.updateAgent(
                      (editingAgent as DepartmentAgent).agent_id,
                      payload,
                    ),
              )
            )
              setEditingAgent(null);
          }}
        >
          <Form.Item
            name="department_id"
            label={t("enterprise.department")}
            rules={[{ required: true }]}
          >
            <Select
              disabled={editingAgent !== "new"}
              options={departments.map((d) => ({
                value: d.department_id,
                label: d.name,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="name"
            label={t("enterprise.name")}
            rules={[{ required: true, whitespace: true }]}
          >
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name="description" label={t("enterprise.description")}>
            <Input.TextArea />
          </Form.Item>
          <Form.Item name="default_model" label={t("enterprise.model")}>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              options={modelOptions}
            />
          </Form.Item>
          <Form.Item name="system_prompt" label={t("enterprise.persona")}>
            <Input.TextArea rows={6} />
          </Form.Item>
          <Form.Item
            name="knowledge_base_ids"
            label={t("enterprise.knowledgeBases")}
          >
            <Select
              mode="multiple"
              options={knowledgeOptions}
              optionFilterProp="label"
              placeholder={t("enterprise.knowledgeHint")}
            />
          </Form.Item>
          <Button htmlType="submit" loading={saving}>
            {t("common.save")}
          </Button>
        </Form>
      </Drawer>
    </Space>
  );
}
