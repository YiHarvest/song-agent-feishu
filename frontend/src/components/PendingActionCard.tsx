import type { PendingAction } from "../types";

interface Props {
  action: PendingAction;
  onConfirm: (id: string) => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
}

export function PendingActionCard({
  action,
  onConfirm,
  onCancel,
  onRetry,
}: Props) {
  const canDecide = action.status === "AWAITING_CONFIRMATION";
  const canRetry = action.status === "FAILED_RETRYABLE";
  return (
    <article className="action-card" data-status={action.status}>
      <header>
        <div>
          <span className="eyebrow">{action.action_type}</span>
          <h3>{String(action.payload.summary ?? "待确认操作")}</h3>
        </div>
        <ExecutionStatus status={action.status} />
      </header>
      <dl>
        {Object.entries(action.payload).map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{Array.isArray(value) ? value.join(", ") : String(value ?? "—")}</dd>
          </div>
        ))}
      </dl>
      {action.error_message && (
        <p className="error-message">{action.error_message}</p>
      )}
      <footer>
        {canDecide && (
          <>
            <button className="primary" onClick={() => onConfirm(action.action_id)}>
              确认
            </button>
            <button onClick={() => onCancel(action.action_id)}>取消</button>
          </>
        )}
        {canRetry && (
          <RetryActionButton onClick={() => onRetry(action.action_id)} />
        )}
      </footer>
    </article>
  );
}

export function ExecutionStatus({ status }: { status: PendingAction["status"] }) {
  return <span className={`status status-${status.toLowerCase()}`}>{status}</span>;
}

export function RetryActionButton({ onClick }: { onClick: () => void }) {
  return (
    <button className="primary" onClick={onClick}>
      重试
    </button>
  );
}
