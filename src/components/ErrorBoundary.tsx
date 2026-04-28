/**
 * Top-level error boundary — wraps the router so a thrown render in any
 * route shows a recoverable surface instead of a blank white screen. The
 * fallback offers a refresh + a path back to /discover (the hero route)
 * and shows the stack trace in dev so we can debug fast.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
  info: ErrorInfo | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, info: null };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.setState({ info });
    console.error("[ErrorBoundary] render threw:", error, info.componentStack);
  }

  reset = () => {
    this.setState({ error: null, info: null });
  };

  render() {
    if (!this.state.error) return this.props.children;

    const isDev = import.meta.env.DEV;
    return (
      <div className="min-h-screen bg-background text-foreground flex items-center justify-center px-6">
        <div className="max-w-xl w-full border border-border p-8 space-y-6">
          <div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground mb-2">
              Something broke
            </div>
            <h1 className="text-2xl font-light tracking-tight">
              Credence hit an unexpected error.
            </h1>
            <p className="text-sm text-muted-foreground mt-3 leading-relaxed">
              The page failed to render. You can try again, or jump back to the
              graph view. If this keeps happening, refresh the tab — local
              snapshot caches will rebuild automatically.
            </p>
          </div>

          {isDev && (
            <pre className="text-mono text-[11px] p-3 border border-border bg-muted/40 overflow-auto max-h-48 whitespace-pre-wrap">
              {this.state.error.message}
              {this.state.info?.componentStack ?? ""}
            </pre>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={this.reset}
              className="border border-foreground bg-foreground text-background px-4 py-2 text-xs uppercase tracking-[0.16em]"
            >
              Try again
            </button>
            <a
              href="/discover"
              className="border border-border px-4 py-2 text-xs uppercase tracking-[0.16em] text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
            >
              Back to pipeline
            </a>
            <button
              onClick={() => window.location.reload()}
              className="text-xs uppercase tracking-[0.16em] text-muted-foreground hover:text-foreground"
            >
              Refresh tab
            </button>
          </div>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
