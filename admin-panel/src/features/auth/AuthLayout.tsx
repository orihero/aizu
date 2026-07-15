import { Outlet } from 'react-router-dom';
import { BrandMark } from '@/shared/ui/BrandMark';

/**
 * Full-screen shell for the public auth routes: the AIZU brand mark above a
 * centered card. The card body is the matched route (login / signup) via Outlet.
 */
export function AuthLayout() {
  return (
    <div className="flex min-h-full items-center justify-center bg-bg px-4 py-12">
      <div className="w-full max-w-[400px]">
        <div className="mb-7 flex flex-col items-center gap-3 text-center">
          <BrandMark tone="canvas" className="size-12" />
          <span className="font-head text-lg font-extrabold tracking-tight text-text">
            AIZU
          </span>
        </div>
        <div className="rounded-card border border-border bg-surface p-7 shadow-lift">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
