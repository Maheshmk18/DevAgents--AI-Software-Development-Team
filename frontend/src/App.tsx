import { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  CheckCircle2,
  GitBranch,
  ListTodo,
  MessageSquareText,
  ShieldCheck,
  Sparkles,
  TestTube,
} from 'lucide-react';

type Task = {
  id: string;
  title: string;
  owner_agent: string;
  priority: 'low' | 'medium' | 'high';
  dependencies: string[];
  status: 'pending' | 'in_progress' | 'completed' | 'blocked';
};

type Run = {
  id: string;
  message: string;
  status: string;
  created_at: string;
  tasks: Task[];
  approvals: unknown[];
  stage_results?: StageResult[];
  tool_events?: ToolEvent[];
  deferred_actions?: DeferredAction[];
  action_results?: ActionResult[];
  final_answer?: string | null;
};

type StageResult = {
  agent: string;
  status: string;
  summary: string;
  details?: Record<string, unknown>;
};

type ToolEvent = {
  agent: string;
  tool: string;
  status: string;
  error?: string;
  via?: string;
};

type DeferredAction = {
  agent?: string;
  tool: string;
  arguments?: Record<string, unknown>;
};

type ActionResult = {
  agent?: string;
  tool: string;
  status: string;
  via?: string;
  error?: string;
};

type McpServer = {
  server: string;
  status?: string;
  tools?: { name: string; description?: string }[];
  error?: string;
};

type McpStatus = {
  enabled: boolean;
  servers: McpServer[];
};

type LlmStatus = {
  configured: boolean;
  primary_provider: string;
  model: string | null;
  last_provider: string | null;
  last_error: string | null;
};

type Approval = {
  id: string;
  run_id: string;
  action: string;
  status: string;
  payload?: Record<string, unknown>;
  arguments?: Record<string, unknown>;
};

type ToolCatalogItem = {
  name: string;
  description: string;
};

type EvaluationResult = {
  run_id: string;
  score: number;
  verdict: string;
  summary: string;
  checks: Array<{ name: string; passed: boolean; score: number }>;
};

type EvaluationHistoryItem = EvaluationResult & {
  created_at?: string;
};

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

async function api<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    throw new Error('Request failed');
  }
  return response.json() as Promise<T>;
}

function App() {
  const [request, setRequest] = useState('Add a /healthcheck endpoint to the API with a pytest covering the success and failure paths.');
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [toolCatalog, setToolCatalog] = useState<ToolCatalogItem[]>([]);
  const [evaluation, setEvaluation] = useState<EvaluationResult | null>(null);
  const [evaluationHistory, setEvaluationHistory] = useState<EvaluationHistoryItem[]>([]);
  const [approvalActionState, setApprovalActionState] = useState<string | null>(null);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [mcpEnabled, setMcpEnabled] = useState(true);
  const [mcpProbing, setMcpProbing] = useState(false);
  const [llmStatus, setLlmStatus] = useState<LlmStatus | null>(null);

  const currentRun = useMemo(() => runs[0] ?? null, [runs]);

  const llmBadge = useMemo(() => {
    if (!llmStatus) {
      return { tone: 'unknown', label: 'checking...' };
    }
    if (!llmStatus.configured) {
      return { tone: 'warn', label: 'not configured' };
    }
    if (llmStatus.last_error) {
      return { tone: 'warn', label: `${llmStatus.primary_provider} · error` };
    }
    return { tone: 'ok', label: llmStatus.primary_provider };
  }, [llmStatus]);

  useEffect(() => {
   void refreshRuns();
  }, []);

  useEffect(() => {
   if (!currentRun) {
     setEvaluation(null);
     return;
   }

   void fetchEvaluation(currentRun.id);
  }, [currentRun?.id]);

  async function refreshRuns() {
    try {
      const [nextRuns, nextApprovals, nextToolCatalog, nextEvaluationHistory, nextHealth] = await Promise.all([
        api<Run[]>('/api/runs'),
        api<Approval[]>('/api/approvals'),
        api<ToolCatalogItem[]>('/api/tools'),
        api<EvaluationHistoryItem[]>('/api/evaluations'),
        api<{ llm: LlmStatus }>('/api/health'),
      ]);
      setRuns(nextRuns);
      setApprovals(nextApprovals);
      setToolCatalog(nextToolCatalog);
      setEvaluationHistory(nextEvaluationHistory);
      setLlmStatus(nextHealth.llm);
    } catch {
      setError('Unable to reach the backend API. Start the backend server first.');
    }
  }

  async function probeMcpServers(probe: boolean) {
    if (probe) {
      setMcpProbing(true);
    }
    try {
      const status = await api<McpStatus>(`/api/mcp/servers?probe=${probe}`);
      setMcpServers(status.servers);
      setMcpEnabled(status.enabled);
    } catch {
      // MCP is optional; keep the last known status instead of failing the UI.
    } finally {
      if (probe) {
        setMcpProbing(false);
      }
    }
  }

  useEffect(() => {
    void probeMcpServers(false);
  }, []);

  async function fetchEvaluation(runId: string) {
    try {
      const result = await fetch(`${API_BASE}/api/evaluations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: runId }),
      });

      if (!result.ok) {
        throw new Error('Failed to evaluate the run');
      }

      const nextEvaluation = (await result.json()) as EvaluationResult;
      setEvaluation(nextEvaluation);
    } catch {
      setEvaluation(null);
    }
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = request.trim();
    if (!value) {
      setError('Please enter a feature or bug request.');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const response = await fetch(`${API_BASE}/api/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: value }),
      });

      if (!response.ok) {
        throw new Error('The backend rejected the request.');
      }

      const run = (await response.json()) as Run;
      setRuns((existing) => [run, ...existing]);
      setRequest('');
      await refreshRuns();
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : 'Could not start the run.');
    } finally {
      setLoading(false);
    }
  }

  async function handleApprovalDecision(approvalId: string, decision: 'approve' | 'reject') {
    setApprovalActionState(approvalId);
    setError('');

    try {
      const response = await fetch(`${API_BASE}/api/approvals/${approvalId}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: decision,
          arguments: {
            approved_by: 'human_operator',
            decision,
          },
        }),
      });

      if (!response.ok) {
        throw new Error('Approval decision could not be recorded.');
      }

      await refreshRuns();
    } catch (approvalError) {
      setError(approvalError instanceof Error ? approvalError.message : 'Approval action failed.');
    } finally {
      setApprovalActionState(null);
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-box">
          <div className="brand-mark">
            <Sparkles size={18} />
          </div>
          <div>
            <h1>AI Dev Team</h1>
            <p>Multi-agent engineering console</p>
          </div>
        </div>

        <div
          className={`llm-badge ${llmBadge.tone}`}
          title={llmStatus?.last_error ?? `model: ${llmStatus?.model ?? 'n/a'}`}
        >
          <span className="llm-dot" />
          <span>LLM: {llmBadge.label}</span>
        </div>

        <nav className="nav">
          <button className="nav-item active" type="button">
            <Activity size={16} />
            Workflow
          </button>
          <button className="nav-item" type="button">
            <ListTodo size={16} />
            Tasks
          </button>
          <button className="nav-item" type="button">
            <ShieldCheck size={16} />
            Approvals
          </button>
          <button className="nav-item" type="button">
            <TestTube size={16} />
            Validation
          </button>
        </nav>

        <div className="module-list">
          <div className="module-header">AGENTS</div>
          {['planner', 'developer', 'reviewer', 'tester', 'docs'].map((agent) => (
            <div key={agent} className="agent-item">
              <span className="agent-dot" />
              {agent}
            </div>
          ))}
        </div>

        <div className="module-list compact">
          <div className="module-header">TOOLS</div>
          {toolCatalog.slice(0, 4).map((tool) => (
            <div key={tool.name} className="module-pill narrow">
              {tool.name}
            </div>
          ))}
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div>
            <p className="eyebrow">Autonomous engineering flow</p>
            <h2>Project orchestration</h2>
          </div>
          <button className="primary-button" type="button">
            <GitBranch size={16} />
            Create project run
          </button>
        </header>

        <form className="composer" onSubmit={handleSubmit}>
          <label htmlFor="request" className="label">Feature or bug request</label>
          <textarea
            id="request"
            value={request}
            onChange={(event) => setRequest(event.target.value)}
            rows={4}
            placeholder="Describe the feature or bug to work on..."
          />
          <div className="composer-actions">
            <button className="primary-button" type="submit" disabled={loading}>
              {loading ? 'Starting...' : 'Launch workflow'}
            </button>
          </div>
        </form>

        {error ? <div className="error-box">{error}</div> : null}

        <section className="dashboard-grid">
          <div className="panel">
            {currentRun ? (
              <>
                <div className="history-panel">
                  <div className="panel-header narrow-header">
                    <Sparkles size={16} />
                    Result
                  </div>
                  <p className="run-request">{currentRun.message}</p>
                  {currentRun.final_answer ? (
                    <p className="run-answer">{currentRun.final_answer}</p>
                  ) : (
                    <p className="empty-state small">
                      The final answer appears once the workflow finishes.
                    </p>
                  )}
                </div>

                <div className="history-panel">
                  <div className="panel-header narrow-header">
                    <GitBranch size={16} />
                    Workflow stages
                  </div>
                  {currentRun.stage_results && currentRun.stage_results.length > 0 ? (
                    <ul className="history-list stage-list">
                      {currentRun.stage_results.map((stage, index) => (
                        <li key={`${stage.agent}-${index}`} className="stage-item">
                          <div className="stage-row">
                            <span className="stage-agent">{stage.agent}</span>
                            <span className="score-pill">{stage.status}</span>
                          </div>
                          {stage.summary ? <p className="stage-summary">{stage.summary}</p> : null}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="empty-state small">No stages yet. Approve the plan to run the workflow.</p>
                  )}
                </div>

                <div className="history-panel">
                  <div className="panel-header narrow-header">
                    <Activity size={16} />
                    Tool activity
                  </div>
                  {currentRun.tool_events && currentRun.tool_events.length > 0 ? (
                    <ul className="history-list">
                      {currentRun.tool_events.map((event, index) => (
                        <li key={`${event.tool}-${index}`}>
                          <span>
                            {event.agent} · {event.tool}
                          </span>
                          <span className="score-pill">
                            {event.error ? 'error' : event.via ? event.via : 'ok'}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="empty-state small">
                      No tools executed yet. Read-only tools run automatically during the workflow.
                    </p>
                  )}
                </div>

                {currentRun.deferred_actions && currentRun.deferred_actions.length > 0 ? (
                  <div className="history-panel">
                    <div className="panel-header narrow-header">
                      <ShieldCheck size={16} />
                      Deferred actions (human gate)
                    </div>
                    <ul className="history-list">
                      {currentRun.deferred_actions.map((action, index) => {
                        const result = (currentRun.action_results ?? []).find(
                          (item) =>
                            item.tool === action.tool &&
                            (item.agent ?? '') === (action.agent ?? ''),
                        );
                        return (
                          <li key={`${action.agent}-${action.tool}-${index}`}>
                            <span>
                              {action.agent} · {action.tool}
                            </span>
                            <span className="score-pill">
                              {result ? result.status : 'awaiting approval'}
                            </span>
                          </li>
                        );
                      })}
                    </ul>
                    <p className="empty-state small">
                      Side-effecting tools wait here for an explicit approve / reject.
                    </p>
                  </div>
                ) : null}

                <div className="history-panel">
                  <div className="panel-header narrow-header">
                    <MessageSquareText size={16} />
                    MCP servers
                  </div>
                  <ul className="history-list">
                    {mcpServers.map((server) => (
                      <li key={server.server}>
                        <span>
                          {server.server}
                          {server.tools && server.tools.length > 0
                            ? ` · ${server.tools.length} tools`
                            : ''}
                        </span>
                        <span className="score-pill">
                          {mcpEnabled ? server.status ?? 'idle' : 'off'}
                        </span>
                      </li>
                    ))}
                    {mcpServers.length === 0 ? (
                      <li>
                        <span>MCP servers not listed yet</span>
                      </li>
                    ) : null}
                  </ul>
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={mcpProbing}
                    onClick={() => void probeMcpServers(true)}
                  >
                    {mcpProbing ? 'Probing...' : 'Probe MCP servers'}
                  </button>
                </div>

                <ul className="task-list">
                  {currentRun.tasks.map((task) => (
                    <li key={task.id}>
                      <span>{task.title}</span>
                      <span className="tag">{task.owner_agent}</span>
                    </li>
                  ))}
                </ul>

                <div className="history-panel">
                  <div className="panel-header narrow-header">
                    <CheckCircle2 size={16} />
                    Evaluation history
                  </div>
                  {evaluationHistory.length > 0 ? (
                    <ul className="history-list">
                      {evaluationHistory.slice(0, 3).map((entry) => (
                        <li key={`${entry.run_id}-${entry.score}`}>
                          <span>{entry.verdict}</span>
                          <span className="score-pill">{entry.score}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="empty-state small">No evaluations recorded yet.</p>
                  )}
                </div>
              </>
            ) : (
              <p className="empty-state">No project runs yet.</p>
            )}
          </div>

          <div className="panel">
            <div className="panel-header">
              <CheckCircle2 size={16} />
              Human review gates
            </div>
            {approvals.length > 0 ? (
              <div className="approval-list">
                {approvals.map((approval) => (
                  <div key={approval.id} className="approval-card">
                    <div className="approval-header">
                      <span>{approval.action}</span>
                      <span className="tag">{approval.status}</span>
                    </div>
                    <div className="approval-actions">
                      <button
                        type="button"
                        className="secondary-button"
                        disabled={approvalActionState === approval.id}
                        onClick={() => void handleApprovalDecision(approval.id, 'approve')}
                      >
                        {approvalActionState === approval.id ? 'Saving...' : 'Approve'}
                      </button>
                      <button
                        type="button"
                        className="ghost-button"
                        disabled={approvalActionState === approval.id}
                        onClick={() => void handleApprovalDecision(approval.id, 'reject')}
                      >
                        Reject
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <ul className="checklist">
                <li>Before code writes</li>
                <li>Before PR / merge actions</li>
                <li>Before deployment</li>
                <li>Before external actions</li>
              </ul>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}

export default App;
