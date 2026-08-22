import { createHashRouter, Navigate, useParams } from 'react-router-dom';
import { AppLayout } from './layout/AppLayout';
import { AuthLayout } from '@/features/auth/AuthLayout';
import { LoginPage } from '@/features/auth/LoginPage';
import { SignupPage } from '@/features/auth/SignupPage';
import { RequireAuth, RedirectIfAuthed, RequireCan, RoleHome } from '@/features/auth/guards';
import { DashboardPage } from '@/features/dashboard/DashboardPage';
import { LeadsPage } from '@/features/leads/LeadsPage';
import { CampaignsPage } from '@/features/campaigns/CampaignsPage';
import { CampaignNewPage } from '@/features/campaigns/CampaignNewPage';
import { CampaignEditPage } from '@/features/campaigns/CampaignEditPage';
import { ReportsPage } from '@/features/reports/ReportsPage';
import { SettingsPage } from '@/features/settings/SettingsPage';
import { AdminAuthProvider } from '@/features/admin/useAdminAuth';
import { RedirectIfAdmin, RequireSuper } from '@/features/admin/AdminGuards';
import { AdminLayout } from '@/features/admin/AdminLayout';
import { AdminLoginPage } from '@/features/admin/AdminLoginPage';
import { FleetPage } from '@/features/admin/FleetPage';
import { OrgsPage } from '@/features/admin/OrgsPage';
import { OrgDetailPage } from '@/features/admin/OrgDetailPage';
import { AuditPage } from '@/features/admin/AuditPage';
import { ModelPerformancePage } from '@/features/admin/ModelPerformancePage';

/**
 * Old /matches/:id deep links fold into the Leads drawer (params can't ride a string
 * redirect). NOTE the legacy id is a bare comment id, which is NOT a lead identity —
 * a lead is (campaignId, platform, commentId). Such a link now lands on the leads list
 * with no drawer instead of opening whichever same-commentId lead sorted first, which
 * is the honest outcome: the URL genuinely does not say which lead it meant.
 */
function RedirectMatchToLead() {
  const { matchId } = useParams();
  return <Navigate to={matchId ? `/leads/${matchId}` : '/leads'} replace />;
}

/** Hash routing keeps deep links working when served as static files. */
export const router = createHashRouter([
  // Public auth routes. An already-signed-in user is bounced to the app.
  {
    element: <RedirectIfAuthed />,
    children: [
      {
        element: <AuthLayout />,
        children: [
          { path: '/login', element: <LoginPage /> },
          { path: '/signup', element: <SignupPage /> },
        ],
      },
    ],
  },
  // Everything else requires a session; anonymous visitors are sent to /login.
  {
    element: <RequireAuth />,
    children: [
      {
        element: <AppLayout />,
        children: [
          // Land each role where it's allowed (member → /leads, others → /dashboard).
          { path: '/', element: <RoleHome /> },
          // Everyone with a session can see leads (member is leads-only).
          { path: '/leads', element: <LeadsPage /> },
          { path: '/leads/:leadId', element: <LeadsPage /> },
          // Role-gated reads — viewer/owner/admin; a member bounces to /leads.
          {
            element: <RequireCan action="view_dashboard" />,
            children: [{ path: '/dashboard', element: <DashboardPage /> }],
          },
          {
            element: <RequireCan action="view_campaigns" />,
            children: [{ path: '/campaigns', element: <CampaignsPage /> }],
          },
          {
            element: <RequireCan action="view_reports" />,
            children: [{ path: '/reports', element: <ReportsPage /> }],
          },
          // Campaign authoring — owner/admin only.
          {
            element: <RequireCan action="edit_campaigns" />,
            children: [
              { path: '/campaigns/new', element: <CampaignNewPage /> },
              { path: '/campaigns/:campaignId/edit', element: <CampaignEditPage /> },
            ],
          },
          // Settings (workspace/team/integrations) — owner/admin only.
          {
            element: <RequireCan action="view_settings" />,
            children: [
              { path: '/settings', element: <SettingsPage /> },
              { path: '/settings/:tab', element: <SettingsPage /> },
            ],
          },
          // Redirects from the pre-Pulse 8-page IA (canonical targets are gated above).
          { path: '/overview', element: <Navigate to="/dashboard" replace /> },
          { path: '/matches', element: <Navigate to="/leads" replace /> },
          { path: '/matches/:matchId', element: <RedirectMatchToLead /> },
          { path: '/watchlist', element: <Navigate to="/leads" replace /> },
          { path: '/review', element: <Navigate to="/leads" replace /> },
          { path: '/health', element: <Navigate to="/reports" replace /> },
          { path: '/spend', element: <Navigate to="/reports" replace /> },
          { path: '/sessions', element: <Navigate to="/reports" replace /> },
          { path: '*', element: <RoleHome /> },
        ],
      },
    ],
  },
  // Superadmin plane — a SEPARATE auth plane (its own cookie + gate), sibling to
  // the org tree above, never nested under org RequireAuth. AdminAuthProvider
  // bootstraps whoami and provides context to the whole /admin subtree.
  {
    path: '/admin',
    element: <AdminAuthProvider />,
    children: [
      {
        element: <RedirectIfAdmin />,
        children: [{ path: 'login', element: <AdminLoginPage /> }],
      },
      {
        element: <RequireSuper />,
        children: [
          {
            element: <AdminLayout />,
            children: [
              { index: true, element: <FleetPage /> },
              { path: 'orgs', element: <OrgsPage /> },
              { path: 'orgs/:orgId', element: <OrgDetailPage /> },
              { path: 'audit', element: <AuditPage /> },
              { path: 'model-performance', element: <ModelPerformancePage /> },
            ],
          },
        ],
      },
    ],
  },
]);
