import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { AgentReadinessBanner } from './AgentReadinessBanner';
import { HaltBanner } from './HaltBanner';
import { ImpersonationBanner } from './ImpersonationBanner';

export function AppLayout() {
  return (
    <div className="flex h-full gap-5 overflow-hidden bg-bg p-5">
      <Sidebar />
      <main className="min-w-0 grow overflow-y-auto">
        <div className="mx-auto max-w-[1380px] pb-12">
          <ImpersonationBanner />
          <HaltBanner />
          <AgentReadinessBanner />
          <Outlet />
        </div>
      </main>
    </div>
  );
}
