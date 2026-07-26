import { FormEvent, useState } from "react";

interface Props {
  onSubmit: (payload: Record<string, unknown>) => Promise<void>;
}

export function CalendarEventEditor({ onSubmit }: Props) {
  const [summary, setSummary] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [saving, setSaving] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    try {
      await onSubmit({
        summary,
        start_time: new Date(start).toISOString(),
        end_time: end ? new Date(end).toISOString() : null,
        timezone: "Asia/Shanghai",
      });
      setSummary("");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="editor" onSubmit={submit}>
      <label>
        日程标题
        <input
          required
          value={summary}
          onChange={(event) => setSummary(event.target.value)}
          placeholder="例如：季度复盘"
        />
      </label>
      <div className="form-grid">
        <label>
          开始
          <input
            required
            type="datetime-local"
            value={start}
            onChange={(event) => setStart(event.target.value)}
          />
        </label>
        <label>
          结束
          <input
            type="datetime-local"
            value={end}
            onChange={(event) => setEnd(event.target.value)}
          />
        </label>
      </div>
      <button className="primary" disabled={saving}>
        {saving ? "准备中…" : "准备创建"}
      </button>
    </form>
  );
}
