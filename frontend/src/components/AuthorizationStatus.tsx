export function AuthorizationStatus({
  authorized,
}: {
  authorized: boolean;
}) {
  return (
    <span className={`authorization ${authorized ? "connected" : "disconnected"}`}>
      {authorized ? "已授权" : "未授权"}
    </span>
  );
}
