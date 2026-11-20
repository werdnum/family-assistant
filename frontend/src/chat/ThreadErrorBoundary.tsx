import React, { Component, type ErrorInfo, type ReactNode } from 'react';

// Error boundary that catches transient @assistant-ui/react tapClientLookup race
// condition errors during rendering (assistant-ui/assistant-ui#3395).
// Without this boundary, the error propagates up and destroys the entire
// React tree. The boundary catches the error, briefly renders null, then
// re-mounts children with a fresh key so the tap system can reinitialize.
interface ThreadErrorBoundaryState {
  hasError: boolean;
  retryKey: number;
}

export class ThreadErrorBoundary extends Component<
  { children: ReactNode },
  ThreadErrorBoundaryState
> {
  state: ThreadErrorBoundaryState = { hasError: false, retryKey: 0 };

  static getDerivedStateFromError(): Partial<ThreadErrorBoundaryState> {
    return { hasError: true };
  }

  componentDidCatch(error: Error, _info: ErrorInfo): void {
    if (
      !error.message?.includes('tapClientLookup') &&
      !error.message?.includes('useClientLookup')
    ) {
      throw error;
    }
    if (this.state.retryKey < 3) {
      queueMicrotask(() => {
        this.setState((s) => ({ hasError: false, retryKey: s.retryKey + 1 }));
      });
    }
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return null;
    }
    return <React.Fragment key={this.state.retryKey}>{this.props.children}</React.Fragment>;
  }
}
