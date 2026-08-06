import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './app/App';
import './index.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root is missing from index.html');
}
const root = createRoot(container);

const THEME_STORAGE_KEY = 'aizu-theme'; // mirrors src/shared/hooks/useTheme.tsx

/**
 * Demo-capture mode (`npm run dev:demo`, `.env.demo` sets VITE_DEMO_CAPTURE=1):
 * dynamically import the seeded fake repository and boot the app pre-authenticated
 * and pre-themed dark, entirely through the existing DI seam — no other code path
 * changes. The dynamic import means a production build never bundles the demo
 * chunk at all (the branch below is dead code once the env var is unset).
 */
async function renderApp() {
  if (import.meta.env.VITE_DEMO_CAPTURE === '1') {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    } catch {
      // private mode — ThemeProvider will just default to light instead
    }
    const { createDemoRepository } = await import('./demo/createDemoRepository');
    root.render(
      <StrictMode>
        <App repository={createDemoRepository()} />
      </StrictMode>,
    );
    return;
  }
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void renderApp();
