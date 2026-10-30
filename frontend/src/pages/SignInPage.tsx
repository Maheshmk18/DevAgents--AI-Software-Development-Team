import { ArrowLeft, ArrowRight, GitBranch, LockKeyhole, ShieldCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { GoogleSignInButton } from '@/pages/GoogleSignInButton';
import './landing.css';

type SignInPageProps = { onSignIn: () => void; error?: string; signedIn?: boolean };

export function SignInPage({ onSignIn, error, signedIn = false }: SignInPageProps) {
  return (
    <main className="signin-page">
      <a className="marketing-brand signin-brand" href="/" aria-label="Back to DevAgents home">
        <span className="marketing-brand-mark"><GitBranch size={19} /></span><span>DevAgents<span className="brand-period">.</span></span>
      </a>
      <section className="signin-layout">
        <div className="signin-copy">
          <a className="signin-back-link" href="/"><ArrowLeft size={15} /> Back to overview</a>
          <span className="section-kicker">YOUR ENGINEERING WORKSPACE</span>
          <h1>Make the next change<br /><em>easier to follow.</em></h1>
          <p>Sign in to start a workflow, follow the agents, and review the result before delivery.</p>
          <div className="signin-points"><span><ShieldCheck size={16} /> Human review stays part of the process</span><span><LockKeyhole size={16} /> Google sign-in only</span></div>
        </div>
        <div className="signin-card">
          <span className="signin-card-symbol"><GitBranch size={22} /></span>
          <span className="section-kicker">DEVAGENTS WORKSPACE</span>
          <h2>{signedIn ? 'You’re signed in.' : 'Welcome back.'}</h2>
          <p>{signedIn ? 'Continue to your workspace to see active runs and approvals.' : 'Use your Google account to securely access your engineering workspace.'}</p>
          {error ? <div className="signin-error" role="alert">{error}</div> : null}
          {signedIn ? (
            <Button className="marketing-primary-cta signin-workspace-button" onClick={onSignIn}>
              Open workspace <ArrowRight size={16} />
            </Button>
          ) : (
            <GoogleSignInButton className="signin-google-button" onClick={onSignIn} />
          )}
          <div className="signin-secure-note"><LockKeyhole size={13} /> No password is stored by DevAgents.</div>
          <div className="signin-card-foot"><span>By continuing, you authorize Google sign-in for this workspace.</span></div>
        </div>
      </section>
      <footer className="signin-footer"><span>© DevAgents</span><span>Private workspace access</span></footer>
    </main>
  );
}
