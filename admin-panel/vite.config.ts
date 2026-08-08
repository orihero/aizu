import { defineConfig } from 'vite';
import type { Connect } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const BRIDGE_SERVER_URL = 'http://127.0.0.1:8765';

// Dev-only routing shim, mirroring what the bridge serves in production now
// that the landing page owns "/" and the SPA shell moved to app/index.html:
//
// 1. "/" -> public/index.html (the landing). Vite's own indexHtmlMiddleware
//    only ever resolves index.html relative to project `root`, never
//    `publicDir` — and its htmlFallbackMiddleware's SPA-fallback rewrite
//    (also root-relative) can't find one there either, now that
//    admin-panel/index.html has moved to admin-panel/app/index.html. Left
//    alone this 404s. Rewriting the URL to "/index.html" up front routes it
//    through servePublicMiddleware instead, which serves the landing
//    (unprocessed, matching how it ships in dist/ — copied verbatim by
//    Vite's public-dir handling, not run through the HTML transform).
// 2. "/app" (no trailing slash) -> redirect to "/app/". Without this, the
//    same htmlFallbackMiddleware tries "/app.html" (doesn't exist), then
//    falls back to root index.html — landing you on the landing page
//    instead of the SPA shell.
//
// Registered via a direct server.middlewares.use() call (not a returned post
// hook), so it runs before Vite's own middlewares, per Vite's configureServer
// convention.
function devLandingAndAppRouting() {
  return {
    name: 'dev-landing-and-app-routing',
    apply: 'serve' as const,
    configureServer(server: { middlewares: Connect.Server }) {
      server.middlewares.use((req, res, next) => {
        // Compare the PATH, not the raw URL: the landing's language switcher
        // accepts "?lang=ru" as an entry point (landing/js/i18n.js), and a
        // literal `req.url === '/'` check 404s on "/?lang=ru". The bridge
        // routes on urlparse(...).path in production, so this matches it.
        const path = (req.url ?? '').split('?')[0];
        if (path === '/') {
          req.url = '/index.html' + (req.url ?? '').slice(1);
          next();
          return;
        }
        if (path === '/app') {
          res.statusCode = 302;
          res.setHeader('Location', '/app/');
          res.end();
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), devLandingAndAppRouting()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      // Dev convenience: the engine bridge serves the JSON API.
      '/api': BRIDGE_SERVER_URL,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      // The marketing landing page (mockups/coreshift-landing, copied verbatim
      // via public/) now owns "/" and ships as dist/index.html straight from
      // public/ with no Rollup involvement. The React SPA shell moved to
      // app/index.html so it no longer collides with that path — pointing
      // Rollup's HTML entry here makes `vite build` emit dist/app/index.html,
      // which the bridge serves for "/app" and "/app/*". Vite's default
      // base ("/") is unchanged, so the shell's absolute /assets/* and
      // /src/main.tsx-built references still resolve correctly from /app/.
      input: {
        app: resolve(__dirname, 'app/index.html'),
      },
      output: {
        manualChunks: {
          charts: ['recharts'],
          vendor: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query', 'zod'],
        },
      },
    },
  },
});
