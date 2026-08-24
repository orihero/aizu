import { defineConfig } from 'vite';
import type { Connect } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { mkdirSync, writeFileSync } from 'node:fs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const BRIDGE_SERVER_URL = 'http://127.0.0.1:8765';

// Where the build parks its source maps. NOT under dist/: in production the bridge
// serves the whole panel dir statically and unauthenticated, so anything that lands
// there is public. `.vite/` is already gitignored repo-wide.
const SOURCEMAP_DIR = resolve(__dirname, '.vite/sourcemaps');

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

// Source maps must never reach dist/.
//
// The production bridge serves the built `admin-panel/dist` as static files with no
// auth in front of it, so a `.map` sitting there is a public download — and a Vite
// source map carries `sourcesContent`, i.e. the COMPLETE original TypeScript. The
// build was shipping 7.5 MB of maps inside a 10 MB dist, and `GET
// /assets/app-*.js.map` handed anyone who could load the panel all 265 sources
// verbatim, the RBAC mirror (`src/shared/auth/roles.ts`) included.
//
// Deleting the maps outright would also delete the ability to read a production stack
// trace, so instead they are kept and RELOCATED. Two halves, both needed:
//
//   * `sourcemap: 'hidden'` — Rollup still generates full maps but emits no
//     `//# sourceMappingURL=` comment, so nothing in the shipped bundle points at
//     them and no browser fetches one automatically.
//   * this plugin — pulls every `.map` OUT of the bundle before Rollup writes it, and
//     writes it to `.vite/sourcemaps/` instead. Removing them from `bundle` (rather
//     than deleting files afterwards) means they are never created under dist/ at
//     all: no window in which a concurrent `rsync`/`docker build`/`aizu panel` could
//     pick one up.
//
// `enforce: 'post'` so this runs after every other plugin has contributed its maps.
function keepSourceMapsOutOfDist() {
  return {
    name: 'sourcemaps-out-of-dist',
    apply: 'build' as const,
    enforce: 'post' as const,
    generateBundle(_options: unknown, bundle: Record<string, unknown>) {
      const moved: string[] = [];
      for (const fileName of Object.keys(bundle)) {
        if (!fileName.endsWith('.map')) continue;
        const asset = bundle[fileName] as { source?: string | Uint8Array };
        const source = asset.source;
        // Only an emitted asset carries `source`; anything else is not ours to move,
        // and dropping it unread would silently lose the map instead of relocating it.
        if (source === undefined) continue;
        const target = resolve(SOURCEMAP_DIR, fileName);
        mkdirSync(dirname(target), { recursive: true });
        writeFileSync(target, typeof source === 'string' ? source : Buffer.from(source));
        delete bundle[fileName];
        moved.push(fileName);
      }
      if (moved.length > 0) {
        // Say where they went: a map you cannot find is a map you cannot use, and the
        // whole point of 'hidden' over `false` is that they stay available offline.
        console.log(
          `\n  source maps (${moved.length}) kept OUT of dist/ → ${SOURCEMAP_DIR}\n`,
        );
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), devLandingAndAppRouting(), keepSourceMapsOutOfDist()],
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
    // 'hidden', not `true`: generate full maps but emit no `//# sourceMappingURL=`
    // pointer into the shipped bundle. The `keepSourceMapsOutOfDist` plugin above then
    // moves the map files themselves out of dist/ entirely. Do not set this back to
    // `true` — that publishes the whole TypeScript source at /assets/*.js.map.
    sourcemap: 'hidden',
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
