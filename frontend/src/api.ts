import type { IdentitySettings, PendingAction } from "./types";

export interface ApplicationResponse {
  status: string;
  message: string;
  action_id?: string;
  data: Record<string, unknown>;
}

const storedIdentity = (): IdentitySettings => {
  const value = localStorage.getItem("song-agent.identity");
  return value
    ? JSON.parse(value)
    : { principalId: "", openId: "", tenantKey: "" };
};

const headers = () => {
  const identity = storedIdentity();
  return {
    "Content-Type": "application/json",
    "X-Principal-Id": identity.principalId,
    "X-Open-Id": identity.openId,
    "X-Tenant-Key": identity.tenantKey,
  };
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { ...headers(), ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return response.json();
}

export const api = {
  pendingActions: () => request<PendingAction[]>("/api/pending-actions"),
  confirm: (id: string) =>
    request(`/api/pending-actions/${id}/confirm`, { method: "POST" }),
  cancel: (id: string) =>
    request(`/api/pending-actions/${id}/cancel`, { method: "POST" }),
  retry: (id: string) =>
    request(`/api/pending-actions/${id}/retry`, { method: "POST" }),
  prepareCalendar: (payload: Record<string, unknown>) =>
    request<ApplicationResponse>("/api/calendar/events/prepare", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  tasks: () => request<ApplicationResponse>("/api/tasks"),
  prepareTask: (payload: Record<string, unknown>) =>
    request<ApplicationResponse>("/api/tasks/prepare", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  completeTask: (guid: string) =>
    request<ApplicationResponse>(`/api/tasks/${guid}/complete/prepare`, {
      method: "POST",
      body: "{}",
    }),
  deleteTask: (guid: string) =>
    request<ApplicationResponse>(`/api/tasks/${guid}/prepare`, {
      method: "DELETE",
      body: "{}",
    }),
  reminders: () => request<ApplicationResponse>("/api/reminders"),
  prepareReminder: (payload: Record<string, unknown>) =>
    request<ApplicationResponse>("/api/reminders/prepare", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  cancelReminder: (eventId: string) =>
    request<ApplicationResponse>(`/api/reminders/${eventId}/prepare`, {
      method: "DELETE",
      body: "{}",
    }),
  chat: (text: string) =>
    request<{ message: string; status: string; action_id?: string }>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  activity: () => request<Array<Record<string, unknown>>>("/api/activity"),
  integration: () =>
    request<{ authorized: boolean; scopes: string[] }>(
      "/api/integrations/feishu/status",
    ),
};
