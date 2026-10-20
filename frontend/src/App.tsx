import { useEffect, useMemo, useState } from 'react';
import {
  ArrowRight,
  CheckCircle2,
  ChevronLeft,
  ChevronDown,
  ChevronRight,
  Clock3,
  Github,
  GitBranch,
  Inbox,
  LayoutDashboard,
  ListTodo,
  Mail,
  Plus,
  ShieldCheck,
  Workflow,
  X,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';

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
  current_stage?: string | null;
  error?: string | null;
  created_at: string;
  tasks: Task[];
  approvals: unknown[];
  stage_results?: StageResult[];
  tool_events?: ToolEvent[];
  deferred_actions?: DeferredAction[];
  action_results?: ActionResult[];
  final_answer?: string | null;
  plan_summary?: string | null;
  repository_slug?: string | null;
  branch_name?: string | null;
  base_branch?: string | null;
  workspace_review?: { changed_files: string[]; diff: string; diff_truncated: boolean } | null;
  pull_request_url?: string | null;
};

type StageResult = {
  agent: string;
  status: string;
  summary: string;
  details?: Record<string, unknown>;
  updated_at?: string | null;
};

type ToolEvent = {
  event_id?: string;
  agent: string;
  tool: string;
  status: string;
  arguments?: Record<string, unknown>;
  result?: unknown;
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
  arguments?: Record<string, unknown>;
  result?: unknown;
  via?: string;
  error?: string;
};

type ConnectionEntry = {
  connected: boolean;
  tone?: 'ok' | 'warn' | 'off';
  note?: string | null;
  where?: string;
  source?: 'oauth' | 'environment' | null;
  account?: string | null;
  sender?: string | null;
  connect_enabled?: boolean;
  repository_selection_enabled?: boolean;
  setup_hint?: string | null;
  callback_url?: string;
};

type Connections = {
  github: ConnectionEntry & { slug?: string | null; owner?: string | null; repo?: string | null };
  gmail: ConnectionEntry & { mode?: string; sender?: string | null };
};

function toneOf(entry: ConnectionEntry | undefined): string {
  if (!entry) return 'off';
  return entry.tone ?? (entry.connected ? 'ok' : 'off');
}

function statusLabel(status?: string | null): string {
  if (!status) return 'Unknown';
  if (status === 'completed_with_errors') return 'Completed with issues';
  if (status === 'approve_plan_before_execution') return 'Plan approval';
  if (status === 'approve_final_delivery' || status === 'execute_deferred_actions') return 'Additional action approval';
  if (status === 'waiting_for_pr_review') return 'Awaiting PR review';
  return status.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusTone(status?: string | null): string {
  const value = (status ?? '').toLowerCase();
  if (['completed', 'complete', 'succeeded', 'approved', 'executed', 'ok'].includes(value)) return 'success';
  if (value === 'completed_with_errors') return 'danger';
  if (['running', 'planning', 'creating_branch', 'in_progress', 'delivering'].includes(value)) return 'active';
  if (['pending', 'awaiting_approval', 'waiting_for_plan_approval', 'waiting_for_action_approval', 'waiting_for_final_approval', 'waiting_for_pr_review'].includes(value)) return 'pending';
  if (['failed', 'error', 'blocked', 'rejected'].includes(value)) return 'danger';
  return 'neutral';
}

function formatDate(value?: string | null): string {
  if (!value) return 'Recently';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? 'Recently'
    : date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

type Approval = {
  id: string;
  run_id: string;
  action: string;
  status: string;
  payload?: Record<string, unknown>;
  arguments?: Record<string, unknown>;
};

type WorkspaceChange = {
  agent?: string;
  path?: string;
  bytes_written?: number | null;
};

type ConnectorProvider = 'github' | 'gmail';

type ConnectorPopupMessage = {
  type: 'ai-dev-team-connector-result';
  provider: ConnectorProvider;
  ok: boolean;
  message: string;
};

type GitHubRepository = {
  full_name: string;
  private: boolean;
  default_branch?: string | null;
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

type AppPage = 'overview' | 'runs' | 'approvals' | 'integrations';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

async function api<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    throw new Error('Request failed');
  }
  return response.json() as Promise<T>;
}

function App() {
  const [request, setRequest] = useState('');
  const [activePage, setActivePage] = useState<AppPage>('overview');
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => window.localStorage.getItem('ai-dev-team-sidebar-collapsed') === 'true',
  );
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [evaluation, setEvaluation] = useState<EvaluationResult | null>(null);
  const [evaluationHistory, setEvaluationHistory] = useState<EvaluationHistoryItem[]>([]);
  const [approvalActionState, setApprovalActionState] = useState<string | null>(null);
  const [connections, setConnections] = useState<Connections | null>(null);
  const [connecting, setConnecting] = useState<ConnectorProvider | null>(null);
  const [githubRepositories, setGithubRepositories] = useState<GitHubRepository[]>([]);
  const [selectedGithubRepo, setSelectedGithubRepo] = useState('');
  const [githubReposLoading, setGithubReposLoading] = useState(false);
  const [savingGithubRepo, setSavingGithubRepo] = useState(false);

  useEffect(() => {
    window.localStorage.setItem('ai-dev-team-sidebar-collapsed', String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  const currentRun = useMemo(
    () => runs.find((run) => run.id === selectedRunId) ?? runs[0] ?? null,
    [runs, selectedRunId],
  );

  // Gates and evaluations belong to ONE run. Listing every approval ever
  // recorded is what filled the panel with decisions from long-finished runs.
  const pendingApprovals = useMemo(
    () => approvals.filter((approval) => approval.status === 'pending'),
    [approvals],
  );
  const awaitingReviewCount = useMemo(() => {
    const runIds = new Set(pendingApprovals.map((approval) => approval.run_id));
    for (const run of runs) {
      if (run.status === 'waiting_for_pr_review') runIds.add(run.id);
    }
    return runIds.size;
  }, [pendingApprovals, runs]);

  const runEvaluations = useMemo(
    () => (currentRun ? evaluationHistory.filter((entry) => entry.run_id === currentRun.id) : []),
    [evaluationHistory, currentRun],
  );

  const activeRunCount = useMemo(
    () => runs.filter((run) => ['running', 'approved', 'planning', 'waiting_for_plan_approval', 'in_progress', 'delivering'].includes(run.status)).length,
    [runs],
  );
  const completedRunCount = useMemo(
    () => runs.filter((run) => ['completed', 'complete', 'succeeded'].includes(run.status)).length,
    [runs],
  );

  useEffect(() => {
   void refreshRuns();
  }, []);

  useEffect(() => {
    function handleConnectorMessage(event: MessageEvent<ConnectorPopupMessage>) {
      let apiOrigin = '';
      try {
        apiOrigin = new URL(API_BASE, window.location.origin).origin;
      } catch {
        return;
      }
      if (event.origin !== apiOrigin || event.data?.type !== 'ai-dev-team-connector-result') return;
      setConnecting(null);
      if (!event.data.ok) {
        setError(event.data.message || 'The account connection was cancelled.');
        return;
      }
      setError('');
      void refreshRuns();
    }

    window.addEventListener('message', handleConnectorMessage);
    return () => window.removeEventListener('message', handleConnectorMessage);
  }, []);

  useEffect(() => {
   if (!currentRun) {
     setEvaluation(null);
     return;
   }

   void fetchEvaluation(currentRun.id);
  }, [currentRun?.id]);

  useEffect(() => {
    setSelectedGithubRepo(connections?.github.slug ?? '');
  }, [connections?.github.account, connections?.github.slug]);

  useEffect(() => {
    setGithubRepositories([]);
    if (!connections?.github.repository_selection_enabled) return undefined;
    let cancelled = false;
    setGithubReposLoading(true);
    api<{ repositories: GitHubRepository[] }>('/api/connectors/github/repositories')
      .then((result) => {
        if (!cancelled) setGithubRepositories(result.repositories);
      })
      .catch(() => {
        if (!cancelled) setGithubRepositories([]);
      })
      .finally(() => {
        if (!cancelled) setGithubReposLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [connections?.github.source, connections?.github.account, connections?.github.repository_selection_enabled]);

  async function refreshRuns() {
    try {
      const [nextRuns, nextApprovals, nextEvaluationHistory, nextConnections] =
        await Promise.all([
          api<Run[]>('/api/runs'),
          api<Approval[]>('/api/approvals'),
          api<EvaluationHistoryItem[]>('/api/evaluations'),
          api<Connections>('/api/connections'),
        ]);
      setRuns(nextRuns);
      setApprovals(nextApprovals);
      setEvaluationHistory(nextEvaluationHistory);
      setConnections(nextConnections);
    } catch {
      setError('Unable to reach the backend API. Start the backend server first.');
    }
  }

  async function clearHistory() {
    if (!window.confirm('Delete every stored run, approval and evaluation?')) {
      return;
    }
    try {
      await fetch(`${API_BASE}/api/runs`, { method: 'DELETE' });
      setEvaluation(null);
      setSelectedRunId(null);
      await refreshRuns();
    } catch {
      setError('Could not clear the run history.');
    }
  }

  async function connectAccount(provider: ConnectorProvider) {
    const popup = window.open('about:blank', `connect-${provider}`, 'popup,width=560,height=720');
    if (!popup) {
      setError('Allow popups for this console, then try connecting again.');
      return;
    }
    setConnecting(provider);
    setError('');
    const popupWatcher = window.setInterval(() => {
      if (popup.closed) {
        window.clearInterval(popupWatcher);
        setConnecting((active) => active === provider ? null : active);
      }
    }, 500);
    try {
      const response = await fetch(`${API_BASE}/api/connectors/${provider}/start`, { method: 'POST' });
      const payload = (await response.json()) as { authorization_url?: string; detail?: string };
      if (!response.ok || !payload.authorization_url) {
        throw new Error(payload.detail || 'Could not start account authorization.');
      }
      popup.location.assign(payload.authorization_url);
    } catch (connectError) {
      popup.close();
      setConnecting(null);
      setError(connectError instanceof Error ? connectError.message : 'Could not connect the account.');
    }
  }

  async function disconnectAccount(provider: ConnectorProvider) {
    setConnecting(provider);
    setError('');
    try {
      const response = await fetch(`${API_BASE}/api/connectors/${provider}`, { method: 'DELETE' });
      const payload = (await response.json()) as { detail?: string; provider_revoked?: boolean };
      if (!response.ok) throw new Error(payload.detail || 'Could not disconnect the account.');
      if (payload.provider_revoked === false) {
        setError('Disconnected locally. Revoke the app from the provider settings if remote revocation is still pending.');
      }
      await refreshRuns();
    } catch (disconnectError) {
      setError(disconnectError instanceof Error ? disconnectError.message : 'Could not disconnect the account.');
    } finally {
      setConnecting(null);
    }
  }

  async function saveGithubRepository() {
    if (!selectedGithubRepo) return;
    setSavingGithubRepo(true);
    setError('');
    try {
      const response = await fetch(`${API_BASE}/api/connectors/github/repository`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug: selectedGithubRepo }),
      });
      const payload = (await response.json()) as { slug?: string; detail?: string };
      if (!response.ok) throw new Error(payload.detail || 'Could not save the agent repository.');
      setSelectedGithubRepo(payload.slug ?? selectedGithubRepo);
      await refreshRuns();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Could not save the agent repository.');
    } finally {
      setSavingGithubRepo(false);
    }
  }

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
      setSelectedRunId(run.id);
      setActivePage('runs');
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

      // Kick off a refresh immediately: if the gate is approved, the workflow now
      // runs in the background, so the console should show "running" and start
      // polling without waiting 6 minutes on a frozen button.
      await refreshRuns();
    } catch (approvalError) {
      setError(approvalError instanceof Error ? approvalError.message : 'Approval action failed.');
    } finally {
      setApprovalActionState(null);
    }
  }

  // Poll the current run whenever it's actively moving (background execution,
  // approvals already kicked off, etc). Stop polling once it settles.
  const shouldPoll = useMemo(() => {
    if (!currentRun) return false;
    const status = currentRun.status;
    return status === 'running' || status === 'planning' || status === 'approved' || status === 'waiting_for_action_approval' || status === 'in_progress' || status === 'delivering';
  }, [currentRun]);

  useEffect(() => {
    if (!shouldPoll) return undefined;
    const interval = window.setInterval(() => {
      void refreshRuns();
    }, 2500);
    return () => window.clearInterval(interval);
  }, [shouldPoll]);

  const pageDetails: Record<AppPage, { title: string; description: string }> = {
    overview: {
      title: 'Workspace overview',
      description: 'Plan, build, and review engineering work in one place.',
    },
    runs: {
      title: 'Workflow runs',
      description: 'Follow agent progress, review outcomes, and open previous work.',
    },
    approvals: {
      title: 'Review queue',
      description: 'Approve the plan before work starts. Finished changes open a pull request for GitHub review.',
    },
    integrations: {
      title: 'Connected accounts',
      description: 'Manage the GitHub and Gmail accounts used by your engineering team.',
    },
  };

  const github = connections?.github;
  const gmail = connections?.gmail;

  function openRun(runId: string) {
    setSelectedRunId(runId);
    setActivePage('runs');
  }

  function renderApprovalCard(approval: Approval) {
    const pending = approval.status === 'pending';
    const isPlanApproval = approval.action === 'approve_plan_before_execution';
    const isDeferredActionApproval = approval.action === 'execute_deferred_actions';
    const proposedActions = Array.isArray(approval.payload?.actions)
      ? (approval.payload.actions as DeferredAction[])
      : [];
    const workspaceChanges = Array.isArray(approval.payload?.workspace_changes)
      ? (approval.payload.workspace_changes as WorkspaceChange[])
      : [];
    const planTasks = Array.isArray(approval.payload?.tasks)
      ? (approval.payload.tasks as Task[])
      : [];
    const review = approval.payload?.workspace_review as Run['workspace_review'];
    const finalApprovalLabel = isDeferredActionApproval ? 'Approve selected actions' : 'Review delivery';
    const relatedRun = runs.find((run) => run.id === approval.run_id);
    const planRepository = approval.payload?.repository_slug ?? relatedRun?.repository_slug ?? github?.slug;
    const canApprovePlan = Boolean(planRepository && github?.connected);

    return (
      <Card asChild className="approval-card" key={approval.id}>
      <article>
        <div className="approval-card-heading">
          <div className="approval-icon"><ShieldCheck size={18} /></div>
          <div className="approval-card-title">
            <strong>{isPlanApproval ? 'Approve engineering plan' : isDeferredActionApproval ? 'Review additional actions' : 'Review delivery'}</strong>
            <span>{relatedRun?.message ?? 'Workflow action'}</span>
          </div>
          <Badge className={`status-pill ${statusTone(approval.status)}`}>{statusLabel(approval.status)}</Badge>
        </div>
        {typeof approval.payload?.notes === 'string' ? (
          <p className={`approval-note ${approval.payload?.report_blocked ? 'approval-warning' : ''}`}>{approval.payload.notes}</p>
        ) : null}
        {isPlanApproval ? (
          <div className="plan-review">
            <div className="approval-final-summary">
              <strong>Plan summary</strong>
              <p>{String(approval.payload?.summary ?? relatedRun?.plan_summary ?? 'The plan is being prepared.')}</p>
            </div>
              <div className="plan-repository"><Github size={15} /><span>Selected repository</span><strong>{String(planRepository ?? 'Choose a GitHub repository in Integrations')}</strong></div>
              {!canApprovePlan ? <button type="button" className="text-button" onClick={() => setActivePage('integrations')}>{github?.connected ? 'Choose a GitHub repository' : 'Connect GitHub'} <ArrowRight size={14} /></button> : null}
            {planTasks.length > 0 ? (
              <ol className="plan-task-list">
                {planTasks.map((task, index) => (
                  <li key={task.id}>
                    <span>{index + 1}</span>
                    <div><strong>{task.title}</strong><small>{statusLabel(task.owner_agent)} · {statusLabel(task.priority)} priority</small></div>
                  </li>
                ))}
              </ol>
            ) : null}
          </div>
        ) : null}
        {typeof approval.payload?.final_answer === 'string' ? (
          <div className="approval-final-summary">
            <strong>Implementation summary</strong>
            <p>{approval.payload.final_answer}</p>
          </div>
        ) : null}
        {workspaceChanges.length > 0 ? (
          <div className="approval-workspace-changes">
            <strong>Changed files on {String(approval.payload?.repository_slug ?? relatedRun?.repository_slug ?? 'selected repository')}</strong>
            <ul>{workspaceChanges.map((change, index) => (
              <li key={`${change.path ?? 'file'}-${index}`}>
                <code>{change.path ?? 'Updated file'}</code>
                <span>{change.agent ?? 'Agent'}{change.bytes_written ? ` · ${change.bytes_written.toLocaleString()} bytes` : ''}</span>
              </li>
            ))}</ul>
            {approval.payload?.branch_name ? <small>Branch: <code>{String(approval.payload.branch_name)}</code> · Base: <code>{String(approval.payload.base_branch ?? 'default branch')}</code></small> : null}
            <small>Review this branch on GitHub after the pull request is created.</small>
          </div>
        ) : null}
        {!isPlanApproval && typeof review?.diff === 'string' ? (
          <details className="diff-review">
            <summary>Review code diff{review.diff_truncated ? ' (preview is truncated)' : ''}</summary>
            <pre>{review.diff}</pre>
          </details>
        ) : null}
        {proposedActions.length > 0 ? (
          <div className="approval-details">
            {proposedActions.map((action, index) => {
              const args = action.arguments ?? {};
              const fileContent = String(args.content ?? '');
              return (
                <div className="approval-action-detail" key={`${action.tool}-${index}`}>
                  <span className="action-agent">{action.agent ?? 'Engineering agent'}</span>
                  <strong>{action.tool.split('.').pop()?.replace(/_/g, ' ') ?? action.tool}</strong>
                  {action.tool === 'gmail.send_email' ? (
                    <div className="email-preview">
                      <div><span>To</span><strong>{String(args.to ?? 'Not specified')}</strong></div>
                      <div><span>Subject</span><strong>{String(args.subject ?? 'Not specified')}</strong></div>
                      <p>{String(args.body ?? '').slice(0, 900)}</p>
                    </div>
                  ) : action.tool === 'repo.write_file' ? (
                    <div className="file-review">
                      <div className="file-review-path"><span>Proposed file</span><strong>{String(args.file_path ?? 'Unknown path')}</strong></div>
                      <details>
                        <summary>Review proposed content ({fileContent.length.toLocaleString()} characters)</summary>
                        <pre>{fileContent.slice(0, 12000)}{fileContent.length > 12000 ? '\n... preview truncated ...' : ''}</pre>
                      </details>
                    </div>
                  ) : action.tool === 'repo.run_command' ? (
                    <div className="command-review"><span>Validation command</span><code>{String(args.command ?? 'No command provided')}</code></div>
                  ) : action.tool === 'github.create_issue' ? (
                    <div className="issue-preview"><strong>{String(args.title ?? 'GitHub issue')}</strong><p>{String(args.body ?? '').slice(0, 900)}</p></div>
                  ) : (
                    <pre>{(JSON.stringify(args, null, 2) ?? '{}').slice(0, 1000)}</pre>
                  )}
                </div>
              );
            })}
          </div>
        ) : null}
        {pending && proposedActions.length === 0 && !isPlanApproval ? <p className="approval-note">No additional external actions were proposed.</p> : null}
        <div className="approval-card-footer">
          {relatedRun ? (
            <button className="text-button" type="button" onClick={() => openRun(relatedRun.id)}>
              View workflow <ArrowRight size={14} />
            </button>
          ) : <span />}
          {pending ? (
            <div className="approval-actions">
              <Button
                type="button"
                variant="secondary"
                className="secondary-button"
                disabled={approvalActionState === approval.id || (isPlanApproval && !canApprovePlan)}
                onClick={() => void handleApprovalDecision(approval.id, 'approve')}
              >
                {approvalActionState === approval.id ? 'Saving...' : isPlanApproval && !canApprovePlan ? github?.connected ? 'Select a repository to continue' : 'Connect GitHub to continue' : isPlanApproval ? 'Approve plan and create branch' : finalApprovalLabel}
              </Button>
              <Button
                type="button"
                variant="ghost"
                className="ghost-button"
                disabled={approvalActionState === approval.id}
                onClick={() => void handleApprovalDecision(approval.id, 'reject')}
              >
                {isPlanApproval ? 'Reject plan' : isDeferredActionApproval ? 'Reject actions' : 'Reject delivery'}
              </Button>
            </div>
          ) : null}
        </div>
      </article>
      </Card>
    );
  }

  return (
    <div className={`app-shell ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
      <aside className="sidebar">
        <div className="sidebar-header">
          <a className="brand-box" href="#" onClick={(event) => { event.preventDefault(); setActivePage('overview'); }}>
            <span className="brand-mark" aria-hidden="true">AD</span>
            <span className="brand-copy">
              <strong>AI Dev Team</strong>
              <small>Engineering workspace</small>
            </span>
          </a>
          <button
            className="sidebar-toggle"
            type="button"
            aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-expanded={!sidebarCollapsed}
            title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
          >
            {sidebarCollapsed ? <ChevronRight size={17} /> : <ChevronLeft size={17} />}
          </button>
        </div>

        <div className="workspace-switcher">
          <span className="workspace-avatar">A</span>
          <span className="workspace-copy"><strong>My workspace</strong><small>Personal workspace</small></span>
          <ChevronDown className="workspace-chevron" size={15} />
        </div>

        <nav className="sidebar-nav" aria-label="Main navigation">
          <span className="nav-section-label">WORKSPACE</span>
          <button type="button" className={`nav-item ${activePage === 'overview' ? 'active' : ''}`} title="Overview" onClick={() => setActivePage('overview')}>
            <LayoutDashboard size={17} /><span>Overview</span>
          </button>
          <button type="button" className={`nav-item ${activePage === 'runs' ? 'active' : ''}`} title="Workflow runs" onClick={() => setActivePage('runs')}>
            <Workflow size={17} /><span>Workflow runs</span><span className="nav-count">{runs.length}</span>
          </button>
          <span className="nav-section-label review-nav-label">REVIEW</span>
          <button type="button" className={`nav-item ${activePage === 'approvals' ? 'active' : ''}`} title="Approvals" onClick={() => setActivePage('approvals')}>
            <ListTodo size={17} /><span>Approvals</span>
            {pendingApprovals.length > 0 ? <span className="nav-count attention">{pendingApprovals.length}</span> : null}
          </button>
          <span className="nav-section-label review-nav-label">CONNECTIONS</span>
          <button type="button" className={`nav-item ${activePage === 'integrations' ? 'active' : ''}`} title="Integrations" onClick={() => setActivePage('integrations')}>
            <GitBranch size={17} /><span>Integrations</span>
            {github?.connected && gmail?.connected ? <span className="nav-connected-dot" title="GitHub and Gmail connected" /> : null}
          </button>
        </nav>

        <div className="sidebar-spacer" />
        <section className="sidebar-accounts">
          <div className="sidebar-section-heading"><span>Connected accounts</span><button type="button" onClick={() => setActivePage('integrations')}>Manage</button></div>
          <div className="sidebar-account-row" title={`GitHub ${github?.connected ? 'connected' : 'not connected'}`}>
            <span className="account-logo github-logo"><Github size={15} /></span>
            <span>GitHub</span>
            <span className={`account-state ${toneOf(github)}`} title={github?.connected ? 'GitHub connected' : connections ? 'GitHub not connected' : 'Checking GitHub'}>{github?.connected ? 'Connected' : connections ? 'Not connected' : 'Checking'}</span>
          </div>
          <div className="sidebar-account-row" title={`Gmail ${gmail?.connected ? 'connected' : 'not connected'}`}>
            <span className="account-logo gmail-logo"><Mail size={15} /></span>
            <span>Gmail</span>
            <span className={`account-state ${toneOf(gmail)}`} title={gmail?.connected ? 'Gmail connected' : connections ? 'Gmail not connected' : 'Checking Gmail'}>{gmail?.connected ? 'Connected' : connections ? 'Not connected' : 'Checking'}</span>
          </div>
          <p>Agents use these accounts for repository work and approved reports.</p>
        </section>
        <div className="sidebar-footer">
          <div className="footer-avatar">A</div>
          <div><strong>Workspace owner</strong><small>Private engineering team</small></div>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div>
            <div className="breadcrumb"><span>My workspace</span><span>/</span><strong>{pageDetails[activePage].title}</strong></div>
            <h1>{pageDetails[activePage].title}</h1>
            <p>{pageDetails[activePage].description}</p>
          </div>
          {activePage !== 'overview' ? (
            <Button className="primary-button topbar-action" type="button" onClick={() => setActivePage('overview')}>
              <Plus size={16} /> New workflow
            </Button>
          ) : null}
        </header>

        {error ? (
          <div className="error-box" role="alert">
            <span>{error}</span>
            <button type="button" aria-label="Dismiss message" onClick={() => setError('')}><X size={16} /></button>
          </div>
        ) : null}

        {activePage === 'overview' ? (
          <div className="page-content overview-page">
            <section className="hero-card">
              <div className="hero-heading">
                <span className="hero-kicker"><GitBranch size={14} /> YOUR ENGINEERING TEAM</span>
                <h2>Turn an idea into a shippable change.</h2>
                <p>Describe what you need. Your agents will plan the work, make progress, and bring important actions back for your approval.</p>
              </div>
              <div className="hero-columns">
                <form className="request-form" onSubmit={handleSubmit}>
                  <label htmlFor="request">What should the team work on?</label>
                  <Textarea
                    id="request"
                    className="request-input"
                    value={request}
                    onChange={(event) => setRequest(event.target.value)}
                    rows={4}
                    placeholder="Describe a feature, bug, or engineering task..."
                  />
                  <div className="request-form-footer">
                    <span><ShieldCheck size={15} /> Other external actions wait for your approval</span>
                    <Button className="primary-button" type="submit" disabled={loading}>
                      {loading ? 'Starting workflow...' : 'Start workflow'} <ArrowRight size={16} />
                    </Button>
                  </div>
                </form>
                <div className="workflow-overview">
                  <h3>From request to result</h3>
                  <ol>
                    <li><span className="step-number">1</span><span><strong>Review plan</strong><small>You approve the tasks before work begins</small></span></li>
                    <li><span className="step-number">2</span><span><strong>Build on a branch</strong><small>Agents implement, review, validate, and document</small></span></li>
                    <li><span className="step-number">3</span><span><strong>Review and validate</strong><small>Agents inspect changes, run checks, and update docs</small></span></li>
                    <li><span className="step-number">4</span><span><strong>Open PR and email report</strong><small>Review on GitHub; receive the PDF in connected Gmail</small></span></li>
                  </ol>
                </div>
              </div>
            </section>

            <section className="metric-grid" aria-label="Workspace summary">
              <article className="metric-card"><span className="metric-icon violet"><Workflow size={18} /></span><div><span>All workflows</span><strong>{runs.length}</strong></div><small>Total runs</small></article>
              <article className="metric-card"><span className="metric-icon blue"><Clock3 size={18} /></span><div><span>In progress</span><strong>{activeRunCount}</strong></div><small>Active right now</small></article>
              <article className="metric-card"><span className="metric-icon amber"><Inbox size={18} /></span><div><span>Awaiting review</span><strong>{awaitingReviewCount}</strong></div><small>PRs and actions need review</small></article>
              <article className="metric-card"><span className="metric-icon green"><CheckCircle2 size={18} /></span><div><span>Completed</span><strong>{completedRunCount}</strong></div><small>Successfully finished</small></article>
            </section>

            <section className="overview-grid">
              <article className="content-card">
                <div className="card-heading">
                  <div><h2>Recent workflows</h2><p>Your latest engineering work</p></div>
                  <button className="text-button" type="button" onClick={() => setActivePage('runs')}>View all <ArrowRight size={15} /></button>
                </div>
                {runs.length > 0 ? (
                  <div className="recent-run-list">
                    {runs.slice(0, 5).map((run) => (
                      <button className="recent-run-row" type="button" key={run.id} onClick={() => openRun(run.id)}>
                        <span className={`run-leading-icon ${statusTone(run.status)}`}><Workflow size={17} /></span>
                        <span className="recent-run-copy"><strong>{run.message}</strong><small>{formatDate(run.created_at)}{run.current_stage ? ` · ${statusLabel(run.current_stage)}` : ''}</small></span>
                        <span className={`status-pill ${statusTone(run.status)}`}>{statusLabel(run.status)}</span>
                        <ArrowRight className="row-arrow" size={16} />
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="empty-panel">
                    <span className="empty-icon"><Workflow size={21} /></span>
                    <strong>Your workspace is ready</strong>
                    <p>Start with a task above. Your runs and results will appear here.</p>
                  </div>
                )}
              </article>

              <article className="content-card review-summary-card">
                <div className="card-heading">
                  <div><h2>Review queue</h2><p>Actions that need your approval</p></div>
                  <span className={`queue-count ${pendingApprovals.length ? 'has-items' : ''}`}>{pendingApprovals.length}</span>
                </div>
                {pendingApprovals.length > 0 ? (
                  <div className="review-preview-list">
                    {pendingApprovals.slice(0, 3).map((approval) => {
                      const relatedRun = runs.find((run) => run.id === approval.run_id);
                      return (
                        <button type="button" className="review-preview-row" key={approval.id} onClick={() => setActivePage('approvals')}>
                          <span className="review-preview-icon"><ShieldCheck size={16} /></span>
                          <span><strong>{statusLabel(approval.action)}</strong><small>{relatedRun?.message ?? 'Workflow action'}</small></span>
                          <ArrowRight size={15} />
                        </button>
                      );
                    })}
                    <button type="button" className="text-button full-link" onClick={() => setActivePage('approvals')}>Open review queue <ArrowRight size={15} /></button>
                  </div>
                ) : (
                  <div className="quiet-empty">
                    <CheckCircle2 size={20} />
                    <strong>Nothing needs your attention</strong>
                    <p>Plan approvals and other external actions appear here. Pull requests are ready for review on GitHub.</p>
                  </div>
                )}
              </article>
            </section>
          </div>
        ) : null}

        {activePage === 'runs' ? (
          <div className="page-content runs-page">
            <div className="page-toolbar">
              <div><span className="section-overline">WORKSPACE ACTIVITY</span><strong>{runs.length} {runs.length === 1 ? 'workflow' : 'workflows'}</strong></div>
              {runs.length > 0 ? <button className="ghost-button" type="button" onClick={() => void clearHistory()}>Clear history</button> : null}
            </div>
            <div className="run-browser">
              <section className="content-card run-browser-list">
                <div className="card-heading"><div><h2>All workflows</h2><p>Select a run to see its full progress</p></div></div>
                {runs.length > 0 ? (
                  <div className="workflow-list">
                    {runs.map((run) => (
                      <button type="button" className={`workflow-list-item ${currentRun?.id === run.id ? 'selected' : ''}`} key={run.id} onClick={() => setSelectedRunId(run.id)}>
                        <span className={`run-leading-icon ${statusTone(run.status)}`}><Workflow size={17} /></span>
                        <span className="workflow-list-copy"><strong>{run.message}</strong><small>{formatDate(run.created_at)}</small><span className={`status-pill ${statusTone(run.status)}`}>{statusLabel(run.status)}</span></span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="empty-panel compact-empty"><span className="empty-icon"><Workflow size={20} /></span><strong>No workflows yet</strong><p>Start a workflow from the overview to see its progress here.</p><button className="text-button" type="button" onClick={() => setActivePage('overview')}>Create a workflow <ArrowRight size={15} /></button></div>
                )}
              </section>

              <section className="content-card run-detail-card">
                {currentRun ? (
                  <>
                    <div className="run-detail-heading">
                      <div><span className="section-overline">WORKFLOW DETAILS</span><h2>{currentRun.message}</h2><p>Started {formatDate(currentRun.created_at)}</p></div>
                      <span className={`status-pill large ${statusTone(currentRun.status)}`}>{statusLabel(currentRun.status)}</span>
                    </div>
                    {currentRun.repository_slug ? (
                      <div className="run-branch-banner">
                        <GitBranch size={16} />
                        <span><strong>{currentRun.repository_slug}</strong>{currentRun.branch_name ? ` · ${currentRun.branch_name}` : ''}</span>
                        {currentRun.pull_request_url ? <a href={currentRun.pull_request_url} target="_blank" rel="noreferrer">Open pull request <ArrowRight size={14} /></a> : null}
                      </div>
                    ) : null}
                    {currentRun.error ? <div className="error-box inline-error">{currentRun.error}</div> : null}
                    {currentRun.status === 'running' && currentRun.current_stage ? <div className="current-progress"><span className="progress-pulse" /><span>Currently working on <strong>{statusLabel(currentRun.current_stage)}</strong></span></div> : null}
                    {currentRun.status === 'delivering' ? <div className="current-progress"><span className="progress-pulse" /><span>Publishing the tested branch and opening a pull request</span></div> : null}
                    <div className="detail-section">
                      <div className="detail-section-heading"><h3>Agent progress</h3><span>{currentRun.stage_results?.length ?? 0} stages</span></div>
                      {currentRun.stage_results && currentRun.stage_results.length > 0 ? (
                        <div className="stage-timeline">
                          {currentRun.stage_results.map((stage, index) => (
                            <article className="stage-card" key={`${stage.agent}-${index}`}>
                              <span className={`stage-marker ${statusTone(stage.status)}`}>{statusTone(stage.status) === 'success' ? <CheckCircle2 size={15} /> : <span />}</span>
                              <div className="stage-card-copy"><div className="stage-card-heading"><strong>{statusLabel(stage.agent)}</strong><span className={`status-pill ${statusTone(stage.status)}`}>{statusLabel(stage.status)}</span></div>{stage.summary ? <p>{stage.summary}</p> : null}</div>
                            </article>
                          ))}
                        </div>
                      ) : <p className="section-empty">{currentRun.status === 'running' ? 'The team is preparing the work plan.' : currentRun.final_answer ? 'No agent work was needed for this message.' : 'Progress details will appear here when agents start working.'}</p>}
                    </div>
                    <div className="detail-section tool-activity-section">
                      <div className="detail-section-heading"><h3>Tool calls</h3><span>{currentRun.tool_events?.length ?? 0} calls</span></div>
                      {currentRun.tool_events && currentRun.tool_events.length > 0 ? (
                        <div className="tool-call-list">
                          {currentRun.tool_events.map((event, index) => {
                            const argumentText = JSON.stringify(event.arguments ?? {}, null, 2) ?? '{}';
                            const resultText = typeof event.result === 'string'
                              ? event.result
                              : JSON.stringify(event.result, null, 2) ?? '';
                            return (
                              <article className="tool-call-card" key={event.event_id ?? `${event.agent}-${event.tool}-${index}`}>
                                <div className="tool-call-main">
                                  <span className={`tool-call-icon ${statusTone(event.status)}`}><GitBranch size={14} /></span>
                                  <span className="tool-call-copy"><strong>{event.tool.replace(/\./g, ' / ')}</strong><small>{statusLabel(event.agent)} agent</small></span>
                                  <span className={`status-pill ${statusTone(event.status)}`}>{event.status === 'ok' ? 'Complete' : statusLabel(event.status)}</span>
                                </div>
                                {event.error ? <p className="tool-call-error">{event.error}</p> : null}
                                {argumentText !== '{}' || resultText ? (
                                  <details className="tool-call-details">
                                    <summary>Call details</summary>
                                    {argumentText !== '{}' ? <div><span>Input</span><pre>{argumentText.slice(0, 4000)}</pre></div> : null}
                                    {resultText ? <div><span>Output</span><pre>{resultText.slice(0, 4000)}</pre></div> : null}
                                  </details>
                                ) : null}
                              </article>
                            );
                          })}
                        </div>
                      ) : (
                        <p className="section-empty">{currentRun.status === 'running' ? 'Tool calls will appear here as the agents inspect, change, and validate the project.' : 'No tools were needed for this workflow.'}</p>
                      )}
                    </div>
                    <div className="detail-section">
                      <div className="detail-section-heading"><h3>Planned tasks</h3><span>{currentRun.tasks.length} tasks</span></div>
                      {currentRun.tasks.length > 0 ? <ul className="task-list">{currentRun.tasks.map((task) => <li key={task.id}><span className={`task-check ${statusTone(task.status)}`}>{statusTone(task.status) === 'success' ? <CheckCircle2 size={16} /> : <span />}</span><span className="task-copy"><strong>{task.title}</strong><small>{statusLabel(task.owner_agent)} - {statusLabel(task.status)}</small></span></li>)}</ul> : <p className="section-empty">Tasks will appear here after the plan is created.</p>}
                    </div>
                    {currentRun.final_answer ? (
                      <div className="result-card"><div className="result-heading"><CheckCircle2 size={17} /><strong>Team report</strong></div><p>{currentRun.final_answer}</p>{currentRun.pull_request_url ? <a href={currentRun.pull_request_url} target="_blank" rel="noreferrer">View pull request <ArrowRight size={14} /></a> : null}</div>
                    ) : null}
                    {runEvaluations.length > 0 ? <div className="evaluation-strip"><span><CheckCircle2 size={16} /> Quality review</span>{runEvaluations.map((entry) => <span className="score-pill" key={`${entry.run_id}-${entry.score}`}>{entry.verdict} - {entry.score}</span>)}</div> : null}
                  </>
                ) : <div className="empty-panel detail-empty"><span className="empty-icon"><Workflow size={22} /></span><strong>Select a workflow</strong><p>Choose a workflow from the list to inspect its plan, progress, and final report.</p></div>}
              </section>
            </div>
          </div>
        ) : null}

        {activePage === 'approvals' ? (
          <div className="page-content approvals-page">
            <section className="approval-intro">
              <span className="approval-intro-icon"><ShieldCheck size={20} /></span>
              <div><h2>Human review gates</h2><p>Approve the plan before work starts. After checks pass, the agent opens a pull request and emails the PDF report to connected Gmail. Other external actions remain here for approval.</p></div>
              <span className={`queue-count large-count ${pendingApprovals.length ? 'has-items' : ''}`}>{pendingApprovals.length}<small>pending</small></span>
            </section>
            <section className="content-card">
              <div className="card-heading"><div><h2>Needs your review</h2><p>Review each proposed action before it runs</p></div></div>
              {pendingApprovals.length > 0 ? <div className="approval-list">{pendingApprovals.map(renderApprovalCard)}</div> : <div className="empty-panel compact-empty"><span className="empty-icon"><CheckCircle2 size={21} /></span><strong>All caught up</strong><p>There are no actions waiting for your approval.</p></div>}
            </section>
            {approvals.some((approval) => approval.status !== 'pending') ? (
              <details className="content-card review-history">
                <summary><span><Clock3 size={17} /> Review history</span><small>{approvals.filter((approval) => approval.status !== 'pending').length} decisions</small></summary>
                <div className="approval-list">{approvals.filter((approval) => approval.status !== 'pending').map(renderApprovalCard)}</div>
              </details>
            ) : null}
          </div>
        ) : null}

        {activePage === 'integrations' ? (
          <div className="page-content integrations-page">
            <section className="integration-callout">
              <div className="integration-callout-icon"><ShieldCheck size={20} /></div>
              <div><strong>Your accounts stay yours</strong><p>Approving a plan lets the agent work in an isolated branch, open a pull request, and email the PDF report to connected Gmail after checks pass. Other external actions still wait for your approval.</p></div>
            </section>
            <div className="integration-grid">
              <article className="content-card integration-card">
                <div className="integration-card-top">
                  <span className="integration-brand github-brand"><Github size={24} /></span>
                  <span className={`status-pill ${toneOf(github) === 'ok' ? 'success' : toneOf(github) === 'warn' ? 'pending' : 'neutral'}`}><span className="status-dot" />{github?.connected ? 'Connected' : connections ? 'Not connected' : 'Checking status'}</span>
                </div>
                <h2>GitHub</h2>
                <p className="integration-description">Give agents access to the repository you choose so they can inspect code and prepare changes.</p>
                <div className="integration-account">
                  <span>Connected account</span>
                  <strong>{github?.account ?? github?.slug ?? (github?.connected ? 'GitHub account connected' : 'No account connected')}</strong>
                </div>
                {github?.repository_selection_enabled ? (
                  <div className="repository-setting">
                    <label htmlFor="github-repository">Change repository</label>
                    <div className="repository-setting-row">
                      <select id="github-repository" value={selectedGithubRepo} onChange={(event) => setSelectedGithubRepo(event.target.value)} disabled={githubReposLoading || githubRepositories.length === 0}>
                        <option value="">{githubReposLoading ? 'Loading repositories...' : 'Choose a repository'}</option>
                        {github.slug && !githubRepositories.some((item) => item.full_name === github.slug) ? <option value={github.slug}>{github.slug} (configured)</option> : null}
                        {githubRepositories.map((repository) => <option key={repository.full_name} value={repository.full_name}>{repository.full_name}{repository.private ? ' - private' : ''}</option>)}
                      </select>
                      <button type="button" className="ghost-button" disabled={!selectedGithubRepo || savingGithubRepo || selectedGithubRepo === github.slug} onClick={() => void saveGithubRepository()}>{savingGithubRepo ? 'Saving...' : 'Save repository'}</button>
                    </div>
                    <p className="repository-setting-help">New workflows will use the saved repository.</p>
                  </div>
                ) : null}
                {!github?.connected && !github?.connect_enabled ? <p className="integration-notice">GitHub setup is not available yet. Ask your workspace administrator to finish connecting this account.</p> : null}
                <div className="integration-actions">
                  <button type="button" className={github?.connected ? 'ghost-button' : 'primary-button'} disabled={!github?.connect_enabled || connecting !== null} onClick={() => void connectAccount('github')}>
                    {connecting === 'github' ? 'Connecting...' : github?.connected && !github.connect_enabled ? 'Connected' : github?.source === 'oauth' ? 'Change GitHub account' : github?.connected ? 'Connect GitHub account' : 'Connect GitHub'}
                  </button>
                  {github?.source === 'oauth' ? <button type="button" className="text-button disconnect-button" disabled={connecting !== null} onClick={() => void disconnectAccount('github')}>Disconnect</button> : null}
                </div>
                {github?.source === 'oauth' ? <p className="github-account-help">Choose another signed-in account in GitHub. If only the current account appears, sign out there and sign in with the account you want.</p> : null}
              </article>

              <article className="content-card integration-card">
                <div className="integration-card-top">
                  <span className="integration-brand gmail-brand"><Mail size={23} /></span>
                  <span className={`status-pill ${toneOf(gmail) === 'ok' ? 'success' : toneOf(gmail) === 'warn' ? 'pending' : 'neutral'}`}><span className="status-dot" />{gmail?.connected ? 'Connected' : connections ? 'Not connected' : 'Checking status'}</span>
                </div>
                <h2>Gmail</h2>
                <p className="integration-description">Connect the mailbox that should receive approved workflow reports and agent messages.</p>
                <div className="integration-account">
                  <span>Connected account</span>
                  <strong>{gmail?.sender ?? (gmail?.connected ? 'Gmail account connected' : 'No account connected')}</strong>
                </div>
                {!gmail?.connected && !gmail?.connect_enabled ? <p className="integration-notice">Gmail setup is not available yet. Ask your workspace administrator to finish connecting this account.</p> : null}
                {gmail?.connected && gmail.note ? <p className="integration-note">This connection may need attention to keep report delivery available.</p> : null}
                <div className="integration-actions">
                  <button type="button" className={gmail?.connected ? 'ghost-button' : 'primary-button'} disabled={!gmail?.connect_enabled || connecting !== null} onClick={() => void connectAccount('gmail')}>
                    {connecting === 'gmail' ? 'Connecting' : gmail?.connected && !gmail.connect_enabled ? 'Connected' : gmail?.connected ? 'Reconnect Gmail' : 'Connect Gmail'}
                  </button>
                  {gmail?.source === 'oauth' ? <button type="button" className="text-button disconnect-button" disabled={connecting !== null} onClick={() => void disconnectAccount('gmail')}>Disconnect</button> : null}
                </div>
              </article>
            </div>
          </div>
        ) : null}
      </main>
    </div>
  );
}

export default App;
