import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";

interface Task {
  guid?: string;
  task_guid?: string;
  summary?: string;
  completed_at?: string;
}

export function TaskEditor() {
  const [summary, setSummary] = useState("");
  const [due, setDue] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [notice, setNotice] = useState("");

  const load = async () => {
    const result = await api.tasks();
    setTasks((result.data.items as Task[] | undefined) ?? []);
  };
  useEffect(() => void load(), []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const result = await api.prepareTask({
      summary,
      due_time: due ? new Date(due).toISOString() : null,
    });
    setNotice(result.message);
    setSummary("");
  };

  const prepare = async (operation: "complete" | "delete", guid: string) => {
    const result =
      operation === "complete"
        ? await api.completeTask(guid)
        : await api.deleteTask(guid);
    setNotice(result.message);
  };

  return (
    <>
      <form className="editor" onSubmit={submit}>
        <label>
          任务标题
          <input required value={summary} onChange={(e) => setSummary(e.target.value)} />
        </label>
        <label>
          截止时间
          <input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
        </label>
        <button className="primary">准备创建</button>
      </form>
      {notice && <p className="reply">{notice}</p>}
      <div className="resource-list">
        {tasks.map((task) => {
          const guid = task.guid ?? task.task_guid ?? "";
          return (
            <article key={guid}>
              <strong>{task.summary ?? guid}</strong>
              <div>
                {!task.completed_at && (
                  <button onClick={() => void prepare("complete", guid)}>准备完成</button>
                )}
                <button onClick={() => void prepare("delete", guid)}>准备删除</button>
              </div>
            </article>
          );
        })}
      </div>
    </>
  );
}
