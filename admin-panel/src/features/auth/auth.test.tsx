import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AppProviders } from '@/app/providers';
import { useAuth } from '@/shared/hooks/useAuth';
import { useDashboard } from '@/shared/hooks/useDashboard';
import { appError, err } from '@/shared/lib/result';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildPanelState } from '@/test/fixtures';
import { AuthLayout } from './AuthLayout';
import { LoginPage } from './LoginPage';
import { SignupPage } from './SignupPage';
import { RequireAuth, RedirectIfAuthed } from './guards';

/** A minimal app mirroring the real route tree: public auth group + gated app group.
 *  Uses the <MemoryRouter> component API (not a data router) so navigation stays
 *  client-side and never builds a fetch Request jsdom can't construct. */
function renderAuthApp(repository: FakePanelRepository, initialPath = '/login') {
  return render(
    <AppProviders repository={repository}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route element={<RedirectIfAuthed />}>
            <Route element={<AuthLayout />}>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/signup" element={<SignupPage />} />
            </Route>
          </Route>
          <Route element={<RequireAuth />}>
            <Route path="/dashboard" element={<div>Dashboard content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
}

function anonymousRepo() {
  const repo = new FakePanelRepository(buildPanelState());
  repo.currentUser = null;
  return repo;
}

describe('LoginPage', () => {
  test('signs in with valid credentials and lands on the gated app', async () => {
    const repo = anonymousRepo();
    const user = userEvent.setup();
    renderAuthApp(repo, '/login');

    await screen.findByRole('heading', { name: 'Welcome back' });
    await user.type(screen.getByLabelText('Email'), 'me@example.com');
    await user.type(screen.getByLabelText('Password'), 'longenough1');
    await user.click(screen.getByRole('button', { name: 'Log in' }));

    expect(await screen.findByText('Dashboard content')).toBeInTheDocument();
    expect(repo.loginAttempts[0]).toEqual({ email: 'me@example.com', password: 'longenough1' });
  });

  test('shows the server error and stays on the form when login is rejected', async () => {
    const repo = anonymousRepo();
    repo.failNextAuth = 'invalid email or password';
    const user = userEvent.setup();
    renderAuthApp(repo, '/login');

    await screen.findByRole('heading', { name: 'Welcome back' });
    await user.type(screen.getByLabelText('Email'), 'me@example.com');
    await user.type(screen.getByLabelText('Password'), 'wrongpassword');
    await user.click(screen.getByRole('button', { name: 'Log in' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('invalid email or password');
    expect(screen.queryByText('Dashboard content')).not.toBeInTheDocument();
  });

  test('blocks submission and shows a field error for an invalid email', async () => {
    const repo = anonymousRepo();
    const user = userEvent.setup();
    renderAuthApp(repo, '/login');

    await screen.findByRole('heading', { name: 'Welcome back' });
    await user.type(screen.getByLabelText('Email'), 'not-an-email');
    await user.type(screen.getByLabelText('Password'), 'longenough1');
    await user.click(screen.getByRole('button', { name: 'Log in' }));

    expect(screen.getByText('Enter a valid email address')).toBeInTheDocument();
    expect(repo.loginAttempts).toHaveLength(0);
  });

  test('toggles password visibility', async () => {
    const repo = anonymousRepo();
    const user = userEvent.setup();
    renderAuthApp(repo, '/login');

    await screen.findByRole('heading', { name: 'Welcome back' });
    const password = screen.getByLabelText('Password');
    expect(password).toHaveAttribute('type', 'password');
    await user.click(screen.getByRole('button', { name: 'Show password' }));
    expect(password).toHaveAttribute('type', 'text');
  });
});

describe('SignupPage', () => {
  test('enforces the minimum password length before calling the server', async () => {
    const repo = anonymousRepo();
    const user = userEvent.setup();
    renderAuthApp(repo, '/signup');

    await screen.findByRole('heading', { name: 'Create your account' });
    await user.type(screen.getByLabelText('Email'), 'new@example.com');
    await user.type(screen.getByLabelText('Password'), 'short');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(screen.getByText('Password must be at least 8 characters')).toBeInTheDocument();
    expect(repo.signupAttempts).toHaveLength(0);
  });

  test('creates an account and lands on the gated app', async () => {
    const repo = anonymousRepo();
    const user = userEvent.setup();
    renderAuthApp(repo, '/signup');

    await screen.findByRole('heading', { name: 'Create your account' });
    await user.type(screen.getByLabelText('Email'), 'new@example.com');
    await user.type(screen.getByLabelText('Password'), 'longenough1');
    await user.type(screen.getByLabelText('Company name'), 'Acme Inc.');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByText('Dashboard content')).toBeInTheDocument();
    expect(repo.signupAttempts[0]?.email).toBe('new@example.com');
    expect(repo.signupAttempts[0]?.companyName).toBe('Acme Inc.');
  });
});

describe('auth route guards', () => {
  test('RequireAuth redirects an anonymous visitor to the login page', async () => {
    renderAuthApp(anonymousRepo(), '/dashboard');
    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument();
  });

  test('RedirectIfAuthed sends an authenticated visitor from /login into the app', async () => {
    // Default FakePanelRepository is authenticated.
    renderAuthApp(new FakePanelRepository(buildPanelState()), '/login');
    expect(await screen.findByText('Dashboard content')).toBeInTheDocument();
  });

  test('a rejecting getCurrentUser still exits the loading state (no stuck spinner)', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.getCurrentUser = () => Promise.reject(new Error('boom'));
    renderAuthApp(repo, '/login');
    // If the bootstrap .catch were missing, status would stay 'loading' forever
    // (RedirectIfAuthed spinner) and the heading would never render.
    expect(await screen.findByRole('heading', { name: 'Welcome back' })).toBeInTheDocument();
  });
});

describe('mid-session expiry', () => {
  function GatedProbe() {
    useDashboard();
    return <div>Gated content</div>;
  }

  test('a 401 from a gated query drops the session and redirects to login', async () => {
    const repo = new FakePanelRepository(buildPanelState()); // authenticated by default
    repo.fetchDashboard = () =>
      Promise.resolve(err(appError('http', 'dashboard request failed (HTTP 401)', 401)));
    render(
      <AppProviders repository={repo}>
        <MemoryRouter initialEntries={['/dashboard']}>
          <Routes>
            <Route element={<RedirectIfAuthed />}>
              <Route element={<AuthLayout />}>
                <Route path="/login" element={<LoginPage />} />
              </Route>
            </Route>
            <Route element={<RequireAuth />}>
              <Route path="/dashboard" element={<GatedProbe />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );
    expect(
      await screen.findByRole('heading', { name: 'Welcome back' }, { timeout: 5000 }),
    ).toBeInTheDocument();
  });
});

describe('useAuth logout', () => {
  function LogoutProbe() {
    const { status, logout } = useAuth();
    return (
      <div>
        <span>status:{status}</span>
        <button type="button" onClick={() => void logout()}>
          do-logout
        </button>
      </div>
    );
  }

  test('logout clears the session and flips status to anonymous', async () => {
    const repo = new FakePanelRepository(buildPanelState()); // authenticated by default
    const user = userEvent.setup();
    render(
      <AppProviders repository={repo}>
        <LogoutProbe />
      </AppProviders>,
    );

    expect(await screen.findByText('status:authenticated')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'do-logout' }));
    expect(await screen.findByText('status:anonymous')).toBeInTheDocument();
    expect(repo.logoutCount).toBe(1);
  });
});
