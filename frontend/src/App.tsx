import { useEffect, useState } from "react";
import {
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
} from "react-router-dom";
import { api } from "./api";
import { ActivityTimeline } from "./components/ActivityTimeline";
import { AuthorizationStatus } from "./components/AuthorizationStatus";
import { CalendarEventEditor } from "./components/CalendarEventEditor";
import { PendingActionCard } from "./components/PendingActionCard";
import { ReminderEditor } from "./components/ReminderEditor";
import { TaskEditor } from "./components/TaskEditor";
import type { IdentitySettings, PendingAction } from "./types";

const navigation = [
  ["/dashboard", "总览"],
  ["/calendar", "日历"],
  ["/tasks", "任务"],
  ["/reminders", "提醒"],
  ["/pending-actions", "待确认"],
  ["/activity", "活动"],
  ["/settings/integrations", "集成"],
];

function Shell() {
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <span>SA</span>
          <div>
            <strong>Song Agent</strong>
            <small>个人工作台</small>
          </div>
        </div>
        <nav>
          {navigation.map(([path, label]) => (
            <NavLink key={path} to={path}>
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main>
        <Outlet />
      </main>
    </div>
  );
}

function Dashboard() {
  const [text, setText] = useState("");
  const [reply, setReply] = useState("");
  return (
    <Page title="今天" subtitle="自然语言和表单走同一套应用服务。">
      <section className="hero">
        <p>现在想安排什么？</p>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            const result = await api.chat(text);
            setReply(result.message);
            setText("");
          }}
        >
          <input
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="十分钟后提醒我喝水"
          />
          <button className="primary">发送</button>
        </form>
        {reply && <p className="reply">{reply}</p>}
      </section>
    </Page>
  );
}

function CalendarPage() {
  const [notice, setNotice] = useState("");
  return (
    <Page title="日历" subtitle="所有写操作先生成待确认动作。">
      <CalendarEventEditor
        onSubmit={async (payload) => {
          const result = (await api.prepareCalendar(payload)) as {
            message: string;
          };
          setNotice(result.message);
        }}
      />
      {notice && <p className="reply">{notice}</p>}
    </Page>
  );
}

function PendingActionsPage() {
  const [actions, setActions] = useState<PendingAction[]>([]);
  const [error, setError] = useState("");
  const load = () => api.pendingActions().then(setActions).catch((e) => setError(e.message));
  useEffect(() => {
    void load();
  }, []);
  const mutate = async (operation: (id: string) => Promise<unknown>, id: string) => {
    await operation(id);
    await load();
  };
  return (
    <Page title="待确认" subtitle="操作状态由后端状态机决定。">
      {error && <p className="error-message">{error}</p>}
      <div className="cards">
        {actions.map((action) => (
          <PendingActionCard
            key={action.action_id}
            action={action}
            onConfirm={(id) => mutate(api.confirm, id)}
            onCancel={(id) => mutate(api.cancel, id)}
            onRetry={(id) => mutate(api.retry, id)}
          />
        ))}
        {!actions.length && !error && <p className="empty">没有待确认操作。</p>}
      </div>
    </Page>
  );
}

function ActivityPage() {
  const [items, setItems] = useState<Array<Record<string, unknown>>>([]);
  useEffect(() => void api.activity().then(setItems), []);
  return (
    <Page title="活动" subtitle="确认、执行和失败记录。">
      <ActivityTimeline items={items} />
    </Page>
  );
}

function IntegrationsPage() {
  const [authorized, setAuthorized] = useState(false);
  const [identity, setIdentity] = useState<IdentitySettings>(() => {
    const value = localStorage.getItem("song-agent.identity");
    return value
      ? JSON.parse(value)
      : { principalId: "", openId: "", tenantKey: "" };
  });
  useEffect(() => {
    if (identity.principalId && identity.openId) {
      void api.integration().then((value) => setAuthorized(value.authorized));
    }
  }, [identity]);
  return (
    <Page title="飞书集成" subtitle="浏览器请求必须显式携带用户身份。">
      <AuthorizationStatus authorized={authorized} />
      <form
        className="editor"
        onSubmit={(event) => {
          event.preventDefault();
          localStorage.setItem("song-agent.identity", JSON.stringify(identity));
          window.location.reload();
        }}
      >
        {(["principalId", "openId", "tenantKey"] as const).map((key) => (
          <label key={key}>
            {key}
            <input
              value={identity[key]}
              onChange={(event) =>
                setIdentity({ ...identity, [key]: event.target.value })
              }
            />
          </label>
        ))}
        <button className="primary">保存身份</button>
      </form>
    </Page>
  );
}

function Page({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <>
      <header className="page-header">
        <div>
          <h1>{title}</h1>
          <p>{subtitle}</p>
        </div>
      </header>
      {children}
    </>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/calendar" element={<CalendarPage />} />
        <Route path="/tasks" element={<Page title="任务" subtitle="确定性任务服务。"><TaskEditor /></Page>} />
        <Route path="/reminders" element={<Page title="提醒" subtitle="提醒使用个人飞书日历执行。"><ReminderEditor /></Page>} />
        <Route path="/pending-actions" element={<PendingActionsPage />} />
        <Route path="/activity" element={<ActivityPage />} />
        <Route path="/settings/integrations" element={<IntegrationsPage />} />
      </Route>
    </Routes>
  );
}
