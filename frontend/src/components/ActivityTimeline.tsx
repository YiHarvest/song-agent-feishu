export function ActivityTimeline({
  items,
}: {
  items: Array<Record<string, unknown>>;
}) {
  return (
    <ol className="timeline">
      {items.map((item) => (
        <li key={String(item.action_id)}>
          <span>{String(item.status)}</span>
          <strong>{String(item.action_type)}</strong>
          <time>
            {new Date(Number(item.created_at) * 1000).toLocaleString("zh-CN")}
          </time>
        </li>
      ))}
    </ol>
  );
}
