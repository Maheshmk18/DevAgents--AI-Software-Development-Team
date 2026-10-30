import { useEffect, useRef, useState } from 'react';
import {
  ArrowRight,
  ArrowUpRight,
  Check,
  CircleCheck,
  ClipboardList,
  Code2,
  FileCheck2,
  FileText,
  FlaskConical,
  GitBranch,
  GitPullRequest,
  LockKeyhole,
  Play,
  ShieldCheck,
  Video,
  X,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { GoogleSignInButton } from '@/pages/GoogleSignInButton';
import './landing.css';

type LandingPageProps = { onSignIn: () => void; signedIn: boolean };

const workflowStages = [
  { title: 'Planner', detail: 'Turns the request into an ordered plan.', icon: ClipboardList },
  { title: 'Developer', detail: 'Implements the approved plan in a branch.', icon: Code2 },
  { title: 'Reviewer', detail: 'Checks the changes and requests fixes.', icon: FileCheck2 },
  { title: 'Tester', detail: 'Runs project checks and records the results.', icon: FlaskConical },
  { title: 'Docs', detail: 'Documents the work and prepares the report.', icon: FileText },
];

const agents = [
  {
    number: '01',
    title: 'Planner',
    description: 'Reads the request and repository, then creates a task plan for you to review.',
    responsibility: 'SETS THE DIRECTION',
    icon: ClipboardList,
    color: 'blue',
  },
  {
    number: '02',
    title: 'Developer',
    description: 'Implements the approved tasks in an isolated workspace and reports the files changed.',
    responsibility: 'IMPLEMENTS THE PLAN',
    icon: Code2,
    color: 'violet',
  },
  {
    number: '03',
    title: 'Reviewer',
    description: 'Checks the diff against the request and sends specific feedback when fixes are needed.',
    responsibility: 'CHECKS THE DIFF',
    icon: FileCheck2,
    color: 'amber',
  },
  {
    number: '04',
    title: 'Tester',
    description: 'Runs the configured validation command and records its actual result.',
    responsibility: 'RUNS VALIDATION',
    icon: FlaskConical,
    color: 'green',
  },
  {
    number: '05',
    title: 'Docs',
    description: 'Updates relevant documentation and prepares a report of the completed work.',
    responsibility: 'RECORDS THE RESULT',
    icon: FileText,
    color: 'slate',
  },
];

const workflowSteps = [
  { number: '01', title: 'Describe the task', description: 'Write a feature or bug request in plain language.', icon: ClipboardList },
  { number: '02', title: 'Approve the plan', description: 'Review the proposed tasks before implementation starts.', icon: ShieldCheck },
  { number: '03', title: 'Build and validate', description: 'Agents implement the change, review the diff, and run checks.', icon: Code2 },
  { number: '04', title: 'Review delivery', description: 'Inspect the diff, then approve pull request creation and report delivery.', icon: GitPullRequest },
];

export function LandingPage({ onSignIn, signedIn }: LandingPageProps) {
  const videoDialogRef = useRef<HTMLDialogElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [videoUnavailable, setVideoUnavailable] = useState(false);

  useEffect(() => {
    if (videoDialogRef.current?.open && !videoUnavailable) {
      videoRef.current?.play().catch(() => {
        // The native video controls remain available if the browser blocks autoplay.
      });
    }
  }, [videoUnavailable]);

  const openWorkflowVideo = () => {
    setVideoUnavailable(false);
    videoDialogRef.current?.showModal();
    videoRef.current?.play().catch(() => {
      // The native video controls remain available if the browser blocks autoplay.
    });
  };

  return (
    <div className="marketing-site">
      <a className="marketing-skip-link" href="#main-content">Skip to content</a>
      <header className="marketing-nav">
        <a className="marketing-brand" href="/" aria-label="DevAgents home">
          <span className="marketing-brand-mark"><GitBranch size={19} strokeWidth={2.3} /></span>
          <span>DevAgents<span className="brand-period">.</span></span>
        </a>
        <nav aria-label="Main navigation" className="marketing-nav-links">
          <a href="#workflow">Workflow</a>
          <a href="#agents">Agents</a>
          <a href="#guardrails">Human review</a>
        </nav>
        <div className="marketing-nav-actions">
          <a className="marketing-signin-link" href={signedIn ? '/app' : '/signin'}>
            {signedIn ? 'Open workspace' : 'Sign in'}
          </a>
          {signedIn ? (
            <Button className="marketing-nav-cta" onClick={onSignIn}>
              Open workspace <ArrowUpRight size={15} />
            </Button>
          ) : (
            <GoogleSignInButton className="marketing-nav-google" onClick={onSignIn} />
          )}
        </div>
      </header>

      <main id="main-content">
        <section className="marketing-hero">
          <div className="marketing-hero-copy">
            <Badge className="marketing-eyebrow">
              <span className="eyebrow-dot" /> LANGGRAPH-ORCHESTRATED ENGINEERING
            </Badge>
            <h1>From engineering request to <span>reviewable pull request.</span></h1>
            <p className="marketing-hero-lede">
              Five role-specific agents plan, implement, review, test, and document a change through a visible LangGraph workflow.
            </p>
            <div className="marketing-hero-actions">
              {signedIn ? (
                <Button className="marketing-primary-cta" onClick={onSignIn}>
                  Open your workspace <ArrowRight size={16} />
                </Button>
              ) : (
                <GoogleSignInButton className="marketing-hero-google" onClick={onSignIn} />
              )}
              <button
                className="marketing-video-button"
                type="button"
                aria-haspopup="dialog"
                aria-controls="workflow-video-dialog"
                onClick={openWorkflowVideo}
              >
                <Play size={15} fill="currentColor" /> Agent workflow video
              </button>
              <a className="marketing-workflow-link" href="#workflow">Explore the workflow <ArrowRight size={15} /></a>
            </div>
            <div className="marketing-hero-footnote">
              <LockKeyhole size={14} /> You approve the plan and delivery before external actions.
            </div>
          </div>

          <div className="hero-workflow-card" role="group" aria-label="The five stages of the DevAgents workflow">
            <div className="hero-workflow-top">
              <span className="hero-workflow-mark"><GitBranch size={15} /></span>
              <div><strong>DevAgents workflow</strong><small>LangGraph state machine</small></div>
              <span className="hero-workflow-count">5 AGENTS</span>
            </div>
            <div className="hero-workflow-stages">
              {workflowStages.map(({ title, detail, icon: Icon }, index) => (
                <div className="hero-workflow-stage" key={title}>
                  <span className="hero-stage-icon"><Icon size={16} /></span>
                  <span className="hero-stage-copy"><strong>{title}</strong><small>{detail}</small></span>
                  {index < workflowStages.length - 1 ? <span className="hero-stage-connector" aria-hidden="true" /> : null}
                </div>
              ))}
            </div>
            <div className="hero-workflow-approval">
              <ShieldCheck size={17} />
              <span><strong>You stay in control</strong><small>Review the plan and approve delivery.</small></span>
              <GitPullRequest size={16} />
            </div>
          </div>
        </section>

        <section className="marketing-proofline" aria-label="Workflow capabilities">
          <span>BUILT INTO EVERY RUN</span>
          <div><ClipboardList size={17} /><strong>Reviewable plan</strong></div>
          <div><Code2 size={17} /><strong>Isolated code changes</strong></div>
          <div><CircleCheck size={17} /><strong>Recorded test results</strong></div>
          <div><GitPullRequest size={17} /><strong>Pull request delivery</strong></div>
        </section>

        <section className="marketing-section workflow-section" id="workflow">
          <div className="section-intro">
            <span className="section-kicker">A CLEAR HANDOFF AT EACH STEP</span>
            <h2>One request.<br /><em>A reviewable result.</em></h2>
            <p>See what each agent is responsible for, what it produced, and where your approval is needed.</p>
          </div>
          <div className="workflow-steps">
            {workflowSteps.map(({ number, title, description, icon: Icon }) => (
              <article className="workflow-step" key={number}>
                <div className="workflow-step-top"><span>{number}</span><Icon size={18} /></div>
                <h3>{title}</h3><p>{description}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="marketing-section agents-section" id="agents">
          <div className="agents-intro">
            <span className="section-kicker">FIVE SPECIALIZED ROLES</span>
            <h2>Meet the agents<br /><em>behind each run.</em></h2>
            <p>Each agent has a focused role and passes its result to the next stage.</p>
          </div>
          <div className="agent-cards">
            {agents.map(({ number, title, description, responsibility, icon: Icon, color }) => (
              <Card className={`agent-card agent-${color}`} key={title}>
                <div className="agent-card-top"><span className="agent-icon"><Icon size={19} /></span><span className="agent-number">{number}</span></div>
                <h3>{title}</h3>
                <p>{description}</p>
                <span className="agent-role-line"><Check size={12} /> {responsibility}</span>
              </Card>
            ))}
          </div>
        </section>

        <section className="principles-section" id="guardrails">
          <div className="principles-heading">
            <span className="section-kicker">AUTOMATION WITH HUMAN OVERSIGHT</span>
            <h2>Move work forward.<br /><em>Keep important decisions yours.</em></h2>
          </div>
          <div className="principles-grid">
            <article><span><ShieldCheck size={19} /></span><h3>Approve before delivery</h3><p>Plan and delivery approvals keep external actions under your control.</p></article>
            <article><span><GitPullRequest size={19} /></span><h3>Inspect every change</h3><p>Review the files and diff before approving pull request creation.</p></article>
            <article><span><CircleCheck size={19} /></span><h3>See real check results</h3><p>The tester reports the command result and the failure details when a check does not pass.</p></article>
          </div>
        </section>

        <section className="marketing-bottom-cta">
          <div>
            <span className="section-kicker">READY TO GET STARTED?</span>
            <h2>Bring your next engineering task.</h2>
            <p>Sign in to follow the workflow and review each handoff.</p>
          </div>
          {signedIn ? (
            <Button className="marketing-primary-cta" onClick={onSignIn}>
              Open your workspace <ArrowRight size={16} />
            </Button>
          ) : (
            <GoogleSignInButton className="marketing-bottom-google" onClick={onSignIn} />
          )}
        </section>
      </main>

      <dialog
        className="workflow-video-dialog"
        id="workflow-video-dialog"
        ref={videoDialogRef}
        aria-labelledby="workflow-video-title"
        onClose={() => {
          videoRef.current?.pause();
          setVideoUnavailable(false);
        }}
        onClick={(event) => {
          if (event.target === event.currentTarget) event.currentTarget.close();
        }}
      >
        <div className="workflow-video-dialog-content">
          <header className="workflow-video-header">
            <div>
              <span className="section-kicker">PRODUCT WALKTHROUGH</span>
              <h2 id="workflow-video-title">Agent workflow video</h2>
              <p>See how a request moves through the five agents.</p>
            </div>
            <button
              className="workflow-video-close"
              type="button"
              aria-label="Close video"
              onClick={() => videoDialogRef.current?.close()}
            >
              <X size={19} />
            </button>
          </header>
          <div className="workflow-video-frame">
            {videoUnavailable ? (
              <div className="workflow-video-placeholder" role="status" aria-live="polite">
                <span><Video size={27} /></span>
                <strong>Your recording will appear here</strong>
                <p>Save your screen recording at this path:</p>
                <code>frontend/public/videos/agent-workflow.mp4</code>
              </div>
            ) : (
              <video
                ref={videoRef}
                className="workflow-video-player"
                controls
                playsInline
                preload="metadata"
                onError={() => setVideoUnavailable(true)}
              >
                <source src="/videos/agent-workflow.mp4" type="video/mp4" />
                Your browser does not support MP4 video playback.
              </video>
            )}
          </div>
          <div className="workflow-video-footnote">
            <LockKeyhole size={14} /> Your video is served from this project; no external video service is used.
          </div>
        </div>
      </dialog>

      <button
        className="marketing-floating-video-button"
        type="button"
        aria-haspopup="dialog"
        aria-controls="workflow-video-dialog"
        onClick={openWorkflowVideo}
      >
        <span className="floating-video-icon"><Play size={16} fill="currentColor" /></span>
        <span className="floating-video-copy"><strong>Agent workflow video</strong><small>See how the agents work</small></span>
      </button>

      <footer className="marketing-footer">
        <a className="marketing-brand" href="/" aria-label="DevAgents home">
          <span className="marketing-brand-mark"><GitBranch size={17} /></span>
          <span>DevAgents<span className="brand-period">.</span></span>
        </a>
        <span>Engineering work, with a clear review path.</span>
        <a href="#workflow">Back to top <ArrowUpRight size={13} /></a>
      </footer>
    </div>
  );
}
