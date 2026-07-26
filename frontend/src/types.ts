export type ActionStatus =
  | "AWAITING_CONFIRMATION"
  | "CONFIRMED"
  | "EXECUTING"
  | "SUCCEEDED"
  | "FAILED_RETRYABLE"
  | "FAILED_FINAL"
  | "CANCELLED"
  | "EXPIRED";

export interface PendingAction {
  action_id: string;
  action_type: string;
  status: ActionStatus;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  error_code: string;
  error_message: string;
  created_at: number;
}

export interface IdentitySettings {
  principalId: string;
  openId: string;
  tenantKey: string;
}
