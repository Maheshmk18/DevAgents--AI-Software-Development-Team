type GoogleSignInButtonProps = {
  onClick: () => void;
  className?: string;
};

export function GoogleSignInButton({ onClick, className = '' }: GoogleSignInButtonProps) {
  return (
    <button
      className={`google-signin-button ${className}`.trim()}
      type="button"
      onClick={onClick}
    >
      <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18">
        <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.27-4.74 3.27-8.1Z" />
        <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.65l-3.57-2.77c-.99.66-2.25 1.06-3.71 1.06-2.85 0-5.27-1.92-6.13-4.5H2.18v2.84A11 11 0 0 0 12 23Z" />
        <path fill="#FBBC05" d="M5.87 14.14A6.6 6.6 0 0 1 5.52 12c0-.74.13-1.46.35-2.14V7.02H2.18A11 11 0 0 0 1 12c0 1.78.43 3.46 1.18 4.98l3.69-2.84Z" />
        <path fill="#EA4335" d="M12 5.36c1.62 0 3.07.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1a11 11 0 0 0-9.82 6.02l3.69 2.84c.86-2.58 3.28-4.5 6.13-4.5Z" />
      </svg>
      <span>Continue with Google</span>
    </button>
  );
}
