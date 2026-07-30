// =============================================================================
// AppErrorBoundary — top-level defense in depth for the operator surface.
//
// A single component render exception must never blank the entire
// application. This boundary catches it, shows a clear operator-visible
// error state, and offers a safe return to the application shell.
//
// It is DEFENSE IN DEPTH: the underlying defect must still be fixed at
// its source (the boundary makes a failure visible and recoverable, not
// acceptable).
//
// Sanitized logging: only the error name, its message, and the React
// component stack are recorded — never request payloads, certificate
// contents, API keys, or other application state. Error messages from
// render failures are type errors ("x.toFixed is not a function"), not
// data dumps, but the message is length-capped as an extra guard.
// =============================================================================

import { Component } from 'react';

function sanitize(text, max = 300) {
  if (typeof text !== 'string') return '';
  return text.slice(0, max);
}

export default class AppErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Sanitized logging path only: name, capped message, component stack.
    // Never log application data, payloads, or configuration.
    console.error(
      '[TCS operator surface] render error caught by boundary:',
      sanitize(error?.name),
      sanitize(error?.message),
      sanitize(info?.componentStack, 1000),
    );
  }

  handleReset = () => {
    this.setState({ error: null });
    // Return to the application shell root. A hard navigation clears
    // any broken in-memory view state that caused the failure.
    window.location.assign('/');
  };

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center p-6">
        <div
          role="alert"
          className="max-w-lg w-full bg-gray-900 border border-red-800 rounded-lg p-6"
        >
          <h1 className="text-lg font-semibold text-red-300 mb-2">
            Display error in the operator surface
          </h1>
          <p className="text-sm text-gray-300 mb-3">
            A view failed to render and was contained by the application
            error boundary. Governance evaluation, certificate issuance,
            and the audit archive are unaffected — this is a display
            failure only.
          </p>
          <p className="text-xs text-gray-500 font-mono mb-4">
            {sanitize(this.state.error?.name) || 'Error'}
            {this.state.error?.message
              ? `: ${sanitize(this.state.error.message, 160)}`
              : ''}
          </p>
          <button
            onClick={this.handleReset}
            className="bg-blue-700 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm"
          >
            Return to application
          </button>
        </div>
      </div>
    );
  }
}
