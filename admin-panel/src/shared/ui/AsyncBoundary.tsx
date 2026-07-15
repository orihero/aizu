import type { ReactNode } from 'react';
import { LoaderCircle, ServerCrash } from 'lucide-react';
import { Button } from './Button';
import { EmptyState } from './EmptyState';

interface AsyncBoundaryProps {
  readonly isLoading: boolean;
  readonly error: Error | null;
  readonly onRetry: () => void;
  readonly children: ReactNode;
}

/** Uniform loading / error / content envelope for every page. */
export function AsyncBoundary({ isLoading, error, onRetry, children }: AsyncBoundaryProps) {
  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-24 text-sm text-text-muted">
        <LoaderCircle className="size-6 animate-spin text-brand" aria-hidden />
        Loading panel state…
      </div>
    );
  }
  if (error) {
    return (
      <EmptyState
        icon={ServerCrash}
        title="Bridge server unavailable"
        description={error.message}
        action={
          <Button variant="ghost" onClick={onRetry}>
            Retry
          </Button>
        }
      />
    );
  }
  return <>{children}</>;
}
