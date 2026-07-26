import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";

interface Reminder {
  event_id?: string;
  summary?: string;
  start_time?: { timestamp?: string };
}

export function ReminderEditor() {
  const [summary, setSummary] = useState("");
  const [start, setStart] = useState("");
  const [items, setItems] = useState<Reminder[]>([]);
  const [notice, setNotice] = useState("");

  const load = async () => {
    const result = await api.reminders();
    setItems((result.data.items as Reminder[] | undefined) ?? []);
  };
  useEffect(() => void load(), []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const result = await api.prepareReminder({
      summary,
      start_time: new Date(start).toISOString(),
      timezone: "Asia/Shanghai",
    });
    setNotice(result.message);
    setSummary("");
  };

  const cancel = async (eventId: string) => {
    const result = await api.cancelReminder(eventId);
    setNotice(result.message);
  };

  return (
    <>
      <form className="editor" onSubmit={submit}>
        <label>
          提醒内容
          <input required value={summary} onChange={(e) => setSummary(e.target.value)} />
        </label>
        <label>
          提醒时间
          <input
            required
            type="datetime-local"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <button className="primary">准备创建</button>
      </form>
      {notice && <p className="reply">{notice}</p>}
      <div className="resource-list">
        {items.map((item) => (
          <article key={item.event_id}>
            <strong>{item.summary ?? item.event_id}</strong>
            <button onClick={() => void cancel(item.event_id ?? "")}>准备取消</button>
          </article>
        ))}
      </div>
    </>
  );
}
