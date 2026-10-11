import { request, requestBlob } from "../request";

export interface Department {
  department_id: string;
  name: string;
  created_at: number;
}

export interface PersonalDefaults {
  name: string;
  default_model: string | null;
  system_prompt: string | null;
}

export interface DepartmentAgent extends PersonalDefaults {
  agent_id: string;
  department_id: string;
  description: string | null;
  knowledge_base_ids: string[];
  enabled: boolean;
  definition_version: number;
}

export interface AssistantStatus {
  agent_id: string | null;
  status: "pending" | "ready" | "failed";
  error: string | null;
}

export const enterpriseApi = {
  departments: () => request<Department[]>("/enterprise/departments"),
  createDepartment: (name: string) =>
    request<Department>("/enterprise/departments", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  renameDepartment: (id: string, name: string) =>
    request<Department>(`/enterprise/departments/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  deleteDepartment: (id: string) =>
    request<void>(`/enterprise/departments/${id}`, { method: "DELETE" }),
  defaults: () => request<PersonalDefaults>("/enterprise/personal-defaults"),
  saveDefaults: (values: PersonalDefaults) =>
    request<PersonalDefaults>("/enterprise/personal-defaults", {
      method: "PUT",
      body: JSON.stringify(values),
    }),
  agents: () => request<DepartmentAgent[]>("/enterprise/agents"),
  createAgent: (
    departmentId: string,
    values: Omit<
      DepartmentAgent,
      "agent_id" | "department_id" | "enabled" | "definition_version"
    >,
  ) =>
    request<DepartmentAgent>(`/enterprise/departments/${departmentId}/agents`, {
      method: "POST",
      body: JSON.stringify(values),
    }),
  updateAgent: (id: string, values: Partial<DepartmentAgent>) =>
    request<DepartmentAgent>(`/enterprise/agents/${id}`, {
      method: "PATCH",
      body: JSON.stringify(values),
    }),
  deleteAgent: (id: string) =>
    request<void>(`/enterprise/agents/${id}`, { method: "DELETE" }),
  retryAssistant: (userId: number) =>
    request<AssistantStatus>(`/enterprise/users/${userId}/assistant/retry`, {
      method: "POST",
    }),
  updateProfile: (
    id: string,
    values: {
      name: string;
      description?: string | null;
      system_prompt?: string | null;
      icon?: string | null;
      icon_name?: string | null;
    },
  ) =>
    request(`/agents/${id}`, { method: "PATCH", body: JSON.stringify(values) }),
  auditFile: (agentId: string, userId: number, path: string) =>
    requestBlob(
      `/agents/${agentId}/workspace/download?as_user=${userId}&path=${encodeURIComponent(
        path,
      )}`,
    ),
  auditThreads: (agentId: string, userId: number) =>
    request<{ thread_id: string; title: string | null }[]>(
      `/agents/${agentId}/threads?as_user=${userId}`,
    ),
  auditHistory: (
    agentId: string,
    userId: number,
    threadId: string,
    offset = 0,
    cursor?: string | null,
  ) =>
    request<{
      messages: {
        id?: string;
        role?: string;
        type?: string;
        content?: unknown;
      }[];
      has_more: boolean;
      next_cursor?: string | null;
      artifacts?: string[];
    }>(
      `/agents/${agentId}/threads/${threadId}/history?as_user=${userId}&offset=${offset}${
        cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""
      }`,
    ),
};
