# Server deployment — aizu.uz production VDS

> **Documented 2026-08-20** by direct inspection of the live box. Everything below was
> observed, not assumed; where a fact could not be verified (files readable only by
> `root`/`aizu`) it is marked **[unverified]**.

`https://aizu.uz` is served by a **single Ubuntu VDS** running one process — the stdlib
bridge (`aizu ... panel`) — which serves both the `/api/*` control plane and the built
panel. Public traffic arrives through a **Cloudflare Tunnel**, not through an open port.
Deploys are automated: push to `main` → GitHub Actions tests, builds, rsyncs, restarts.

---

## 1. Topology

```
browser ──https──▶ Cloudflare edge ──┐
                                     │  (outbound tunnel, no inbound ports)
                                     ▼
                         ┌───────────────────────────────┐
                         │  VDS  192.166.228.52           │
                         │  hostname: abdu-test           │
                         │                                │
                         │  cloudflared.service (root)    │
                         │        │                       │
                         │        ▼                       │
                         │  aizu.service (user: aizu)     │
                         │  127.0.0.1:8765                │
                         │    ├── /            landing    │
                         │    ├── /app/        SPA        │
                         │    └── /api/*       control    │
                         │        │                       │
                         │        ▼                       │
                         │  /var/lib/aizu/aizu.db (SQLite)│
                         │                                │
                         │  caddy.service — see §4, NOT   │
                         │  in the request path today     │
                         └───────────────────────────────┘
```

**No worker runs on this box.** The only aizu units are `aizu.service` and
`aizu-backup.{service,timer}` — there is no `aizu-worker`. Harvest jobs need a worker
fleet elsewhere (see `docs/architecture/overview.md`); this VDS is control plane only.

### Host facts

| | |
|---|---|
| IP / SSH port | `192.166.228.52`, port **722** (NAT'd to the box's sshd on 22) |
| Hostname | `abdu-test` — a leftover name; **this is production** |
| OS / kernel | Ubuntu, Linux 6.8.0-137-generic, x86_64 |
| Python | 3.12.3 |
| Resources | 1.9 GiB RAM, 2 GiB swap, 38 GB disk (17% used) |
| Deploy user | `developer` (owns `/opt/aizu`) |
| Service user | `aizu` (owns `/var/lib/aizu`, `/var/log/aizu`, `/var/backups/aizu`) |

---

## 2. Request path — how traffic actually reaches the app

`aizu.uz` resolves to **Cloudflare** (`172.67.180.62`, `104.21.35.223`), not to the VDS.
The origin is reached over a **Cloudflare Tunnel**:

```
/etc/systemd/system/cloudflared.service
  /usr/bin/cloudflared --no-autoupdate tunnel run --token-file /etc/cloudflared/token
```

- The tunnel is **token-based and dashboard-managed** — `/etc/cloudflared/` holds only
  `token` (no `config.yml`), so the *public hostname → local service* mapping lives in the
  **Cloudflare Zero Trust dashboard**, not in this repo or on the box.
- Health: `curl -s http://127.0.0.1:20241/ready` → `{"status":200,"readyConnections":4,...}`.
  Port 20241 is cloudflared's metrics/readiness listener (`/metrics` too).
- `cloudflared-update.timer` keeps the binary current.

**Consequence:** ports 80/443 are *not* reachable from the public internet (verified — a
direct `curl http://192.166.228.52/` from outside times out, while the same request through
`https://aizu.uz` lands in the bridge's journal). The tunnel is the only ingress. Killing
`cloudflared` takes the site down even though `aizu.service` stays healthy.

**Verification trick used here** (useful when debugging "is my request reaching the box?"):
`curl -I https://aizu.uz/` from your laptop, then `journalctl -u aizu -n 5` on the box —
the matching `HEAD / → 200` line proves the path end to end.

---

## 3. The application service

```
/etc/systemd/system/aizu.service
```

```ini
User=aizu  Group=aizu
WorkingDirectory=/var/lib/aizu
EnvironmentFile=/etc/aizu/aizu.env
ExecStart=/opt/aizu/venv/bin/aizu --db /var/lib/aizu/aizu.db panel \
    --host 127.0.0.1 --port 8765 \
    --panel-dir /opt/aizu/panel \
    --config /var/lib/aizu/config
Restart=on-failure   RestartSec=3   TimeoutStopSec=20
```

Note the argument order — `--db` precedes the `panel` subcommand, as the CLI requires
(see `CLAUDE.md`; trailing it dies with `unrecognized arguments`).

Hardened with `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `ProtectSystem=strict`,
`ProtectHome`, `ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`,
`RestrictSUIDSGID`, `RestrictRealtime`, `LockPersonality`. Writable paths are limited to:

```
ReadWritePaths=/var/lib/aizu /var/log/aizu /var/backups/aizu
```

⚠️ **Adding a new write target means editing this line**, or the write fails with a
read-only-filesystem error that looks nothing like a permissions bug.

### Filesystem layout

| Path | Owner | Contents |
|---|---|---|
| `/opt/aizu/engine/` | `developer` | Engine source, rsynced from the repo's `engine/` |
| `/opt/aizu/panel/` | `developer` | Built panel — `index.html` (landing), `app/`, `assets/`, `landing/` |
| `/opt/aizu/venv/` | `developer` | Python 3.12 venv; `aizu` installed **editable** (`__editable__.aizu-0.1.0.pth` → `/opt/aizu/engine`) |
| `/var/lib/aizu/aizu.db` | `aizu` | SQLite — all state **[unverified: not readable as `developer`]** |
| `/var/lib/aizu/config/` | `aizu` | Campaign briefs (`--config`) **[unverified]** |
| `/etc/aizu/aizu.env` | `root` | Secrets / env **[unverified: `700` on `/etc/aizu`]** |
| `/var/log/aizu/` | `aizu` | App log **[unverified]** |
| `/var/backups/aizu/` | `aizu` | Nightly SQLite snapshots **[unverified]** |

Because the install is **editable**, rsyncing source into `/opt/aizu/engine/` plus a
restart is a complete code deploy — no reinstall needed unless `pyproject.toml` changed.

### Environment / secrets

`/etc/aizu/aizu.env` is not readable by the `developer` account, so its contents were not
inspected. Per `CLAUDE.md` a hosted deployment needs at least:

- `AIZU_SECRET_KEY` — Fernet key; required for integrations and admin bootstrap/login
- `AIZU_ALLOWED_ORIGINS` — must include `https://aizu.uz` (without it every panel POST
  from a network-served page gets `403 cross-origin request rejected`)
- `AIZU_ADMIN_IP_ALLOWLIST` — **fail-closed** by default: empty/unset means no superadmin
  access at all. **Set to `0.0.0.0/0,::/0` on 2026-08-20 — the admin plane is open to any
  source IP. See §5.**
- `AIZU_TRUSTED_PROXIES` — set to `127.0.0.1` so cloudflared's `X-Forwarded-For` is
  authoritative; without it every request looks like it came from loopback
- `OPENROUTER_API_KEY` — required for live runs and AI campaign generate/interview
- Optional: Polar billing, per-platform creds, `AIZU_LOG_*`

To read or edit it you need root: `sudo -e /etc/aizu/aizu.env`, then
`sudo systemctl restart aizu`.

---

## 4. Caddy — installed, configured, and **not in the request path**

`caddy.service` is active and listens on `:80`/`:443`, but **no production traffic flows
through its `aizu.uz` site block**. Evidence:

1. `/var/log/caddy/aizu.log` — the access log for the `aizu.uz`/`www.aizu.uz` block — is
   **0 bytes, mtime Aug 7**, despite the site serving traffic.
2. A local TLS handshake to `:443` with SNI `aizu.uz` **fails** — Caddy never obtained a
   certificate (`/config/apps/tls` is `null`), because ACME validation can't complete
   behind a proxied Cloudflare record with no inbound ports.
3. `:80` with `Host: aizu.uz` returns **308 → https://aizu.uz/** (Caddy's auto-HTTPS
   redirect), so it could not be serving the tunnel either.
4. The only vhost that answers is the block matching `Host: 192.166.228.52` on `:80`.

### It is actively failing, every 6 hours

Caddy is still trying to obtain the certificate it will never get, and the failure proves
the request path:

```
attempt: 47   retrying_in: 21600s   elapsed: ~6.25 days   max_duration: 30 days
ca: https://acme-staging-v02.api.letsencrypt.org/directory
error: Invalid response from http://aizu.uz/.well-known/acme-challenge/<token>:
       "<!doctype html>\n<html lang=\"en\">..."
```

Let's Encrypt fetched the HTTP-01 challenge over the public name, and got **the panel's
landing page** back instead of the challenge token. That means the challenge request was
routed to the bridge, not to Caddy — Caddy owns the `/.well-known/acme-challenge/` route
for its own sites and would have answered it correctly had it received it.

Note the CA: Caddy has already fallen back to **`acme-staging`**, i.e. it burned through
Let's Encrypt's production rate limits before giving up. It has produced ~1,073 journal
lines of this in 7 days and will keep retrying for the full 30-day window.

### Why it exists at all

Timestamps tell the story:

| Time (2026-08-07) | Event |
|---|---|
| 15:01 | `/etc/caddy/Caddyfile` written — reverse proxy + automatic TLS for `aizu.uz`, plus a temporary cleartext-by-IP vhost |
| 17:40 | `/etc/cloudflared/` created — the tunnel |

Caddy was set up **first**, for a design where the VDS faced the internet directly and
needed its own TLS. Two and a half hours later the Cloudflare Tunnel replaced that design:
TLS now terminates at Cloudflare's edge, and the box has no inbound ports at all. Both of
Caddy's jobs disappeared, but the service was left running.

What it still provides today: `zstd`/`gzip` compression (which Cloudflare's edge does
anyway) and, at most, one extra proxy hop. It costs ~57 MB RSS on a 1.9 GiB box.

### Removing it — order matters

The tunnel's ingress is dashboard-managed, so **check the target first**: Cloudflare Zero
Trust → Networks → Tunnels → the tunnel → Public Hostnames → *Service*.

- If it reads `http://localhost:8765` — Caddy is already bypassed entirely. Disabling it is
  a no-op for traffic: `sudo systemctl disable --now caddy`.
- If it reads `http://192.166.228.52:80` (or similar) — Caddy is a live hop. **Repoint the
  service to `http://localhost:8765` and verify the site first**, then disable Caddy.

Either way, do not simply delete the "TEMPORARY" IP block while leaving Caddy in the path —
that is the only vhost still capable of answering, so removing it is what would actually
break the site.

If Caddy is kept for any reason, at minimum stop the ACME churn by making the sites
internal-only (`tls internal`) or removing the `aizu.uz` block, since a public certificate
is unobtainable behind a proxied Cloudflare record with no inbound ports.

---

## 5. Superadmin plane — IP filtering removed (2026-08-20)

The superadmin plane (`/api/admin/*`) is a separate, higher-privilege auth surface from the
org `/api/*` cookie plane — the app's one sanctioned, audited cross-org BOLA bypass. It
shipped with three gates. **The IP allowlist was deliberately opened on 2026-08-20 at the
operator's request.**

### Current posture

```
AIZU_ADMIN_IP_ALLOWLIST=0.0.0.0/0,::/0     # admit any host
AIZU_TRUSTED_PROXIES=127.0.0.1             # trust the local tunnel's XFF
```

`0.0.0.0/0,::/0` is the value `admin_auth.py` documents for "admit any host", so the gate
remains in the code path and can be narrowed again by editing one line — no deploy needed.
Applied via `/etc/aizu/aizu.env` + `systemctl restart aizu` (the file is read only at
service start).

**Verified from the public internet**: `POST https://aizu.uz/api/admin/login` with `{}`
returns `400 invalid email` rather than `403 forbidden`, i.e. the request cleared the IP
gate and reached payload validation. (Sending `{}` touches no account and consumes no
lockout counter — a useful, side-effect-free probe.)

### What still protects the plane

| Control | Detail |
|---|---|
| **TOTP MFA** | RFC 6238, 6 digits, 30s step, ±1 step skew window. Required on *every* login |
| **Per-email lockout** | DB-backed (`admin_login_is_locked`) → `429 too many failed attempts` |
| **Opaque errors** | Bad password, bad code, missing admin, and disabled admin all return `invalid credentials` |
| **Timing safety** | A dummy hash is verified when the admin doesn't exist, so response time doesn't disclose account existence |
| **Short sessions** | 12h TTL (vs 30 days for org sessions), because the bypass is high-value |
| **Audit trail** | Every login and logout is recorded with IP and user-agent |

The lockout is keyed by **email**, so it stops credential stuffing against a known account
but not distributed enumeration across many addresses.

### Why `AIZU_TRUSTED_PROXIES` was set at the same time

Every request reaches the bridge from `127.0.0.1` (cloudflared dials the origin locally).
Before this change the admin audit log stamped `127.0.0.1` on every entry — and once IP no
longer gates *access*, that audit trail is the only remaining record of where a superadmin
logged in from. Trusting the local tunnel restores real client IPs.

This is safe **because** the allowlist no longer gates on IP: a spoofed `X-Forwarded-For`
grants no access, it only writes a wrong audit line. ⚠️ **If the allowlist is ever narrowed
again, revisit this** — with a restrictive allowlist, a trusted-proxy misconfiguration
becomes an authentication bypass.

### Reverting

Restore the timestamped backup written next to the env file and restart:

```sh
ssh -t aizu 'sudo cp /etc/aizu/aizu.env.bak-<stamp> /etc/aizu/aizu.env && sudo systemctl restart aizu'
```

Or narrow it in place — `AIZU_ADMIN_IP_ALLOWLIST=<office-ip>/32,<vpn-cidr>` — and restart.
Verify either way with the `400`-vs-`403` probe above.

---

## 6. Deploy pipeline (GitHub Actions)

`.github/workflows/ci-cd.yml` — triggers on push to `main`, PRs (tests only), and
`workflow_dispatch`. `concurrency: ci-cd-<ref>` with `cancel-in-progress: false` so two
deploys never race onto the box.

| Job | What it does |
|---|---|
| `engine` | `pip install -e "./engine[dev,telegram]"` → `python -m pytest -q` (15-min hard timeout — a *hung* test would otherwise block deploys for GitHub's 6-hour default) |
| `panel` | `npm ci` → `typecheck` → `lint` → `test` → `build` → uploads `panel-dist` artifact |
| `deploy` | `needs: [engine, panel]`, skipped on PRs, environment `production` |

The `deploy` job:

1. Writes `secrets.DEPLOY_SSH_KEY` + `DEPLOY_KNOWN_HOSTS` into `~/.ssh/`.
2. `rsync -az --delete` the repo's `engine/` → `/opt/aizu/engine/`, excluding
   `__pycache__/`, `*.pyc`, `*.egg-info/`, `*.db`, `*.log`, `run-logs/`, `.venv/`.
   **The `*.db` exclude is load-bearing** — without it `--delete` semantics plus a stray
   local DB could clobber production data.
3. `rsync -az --delete` the built `admin-panel/dist/` → `/opt/aizu/panel/`.
4. `pip install --quiet --upgrade-strategy only-if-needed -e /opt/aizu/engine` (picks up
   dependency changes) → `sudo -n /usr/bin/systemctl restart aizu`.
5. Health check: polls `http://127.0.0.1:8765/` for up to 20s, requires HTTP 200, else
   fails the job.

### Required GitHub secrets

`DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_HOST`, `DEPLOY_PORT`, `DEPLOY_USER`.

### Why the restart works without a password

`developer` has a scoped NOPASSWD grant:

```
(root) NOPASSWD: /usr/bin/systemctl restart aizu, status aizu, start aizu, stop aizu, is-active aizu
```

That is exactly what steps 4–5 use. (`developer` *also* has unrestricted `(ALL : ALL) ALL`
sudo, which requires a password — see §8.)

### Authorized keys on the box

Two, both ED25519 — the CI deploy key is correctly separate from the human one:

| Fingerprint | Comment |
|---|---|
| `SHA256:V34zrFYN55UZBtrN62zgSsx0p87kFIamQfk0DRpyKVM` | `aka.orihero@gmail.com` (operator) |
| `SHA256:P2M93kXoRbAuNSBk44hy6guiJ4O4lQ5evEJ1l4TNUsQ` | `github-actions-deploy@aizu` (CI) |

---

## 7. Backups

`aizu-backup.timer` → `OnCalendar=*-*-* 03:30:00`, `Persistent=true`,
`RandomizedDelaySec=300`. Last run 2026-08-20 03:31, exit status **0**.

`/usr/local/bin/aizu-backup`:

```bash
sqlite3 /var/lib/aizu/aizu.db ".backup '/var/backups/aizu/aizu-<stamp>.db'"
gzip -9 …; find /var/backups/aizu -name 'aizu-*.db.gz' -mtime +14 -delete
```

Uses SQLite's `.backup`, so it is **safe against a live writer** — not a `cp`. Retention is
14 days. ⚠️ Snapshots live **on the same disk as the database**; there is no offsite copy
(§9).

---

## 8. Runbook

All commands assume an SSH alias. Add to `~/.ssh/config`:

```
Host aizu
    HostName 192.166.228.52
    Port 722
    User developer
    IdentityFile ~/.ssh/aizu_server
    IdentitiesOnly yes
```

| Task | Command |
|---|---|
| Status | `ssh aizu 'systemctl status aizu --no-pager'` |
| Live logs | `ssh aizu 'journalctl -u aizu -f'` |
| Recent errors | `ssh aizu 'journalctl -u aizu -p warning -n 50 --no-pager'` |
| Restart | `ssh aizu 'sudo -n systemctl restart aizu'` (NOPASSWD) |
| Local health | `ssh aizu 'curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/'` |
| Tunnel health | `ssh aizu 'curl -s http://127.0.0.1:20241/ready'` |
| Public health | `curl -I https://aizu.uz/` then check the journal for the matching line |
| Deployed version | `ssh aizu 'ls -la /opt/aizu/panel/index.html'` — mtime = last deploy (see §8: no version marker) |
| Edit env | `ssh -t aizu 'sudo -e /etc/aizu/aizu.env'` then restart |
| Manual backup | `ssh aizu 'sudo -u aizu /usr/local/bin/aizu-backup'` |

### Deploying

Normal path: **merge to `main` and let Actions run.** Watch the run; the health check gates
success.

Manual deploy (only if Actions is down) — from a clean checkout, with the panel built:

```sh
cd admin-panel && npm run build && cd ..
rsync -az --delete --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.egg-info/' \
  --exclude='*.db' --exclude='*.log' --exclude='run-logs/' --exclude='.venv/' \
  -e 'ssh -p 722' engine/ developer@192.166.228.52:/opt/aizu/engine/
rsync -az --delete -e 'ssh -p 722' admin-panel/dist/ developer@192.166.228.52:/opt/aizu/panel/
ssh aizu '/opt/aizu/venv/bin/pip install -q -e /opt/aizu/engine && sudo -n systemctl restart aizu'
```

Keep the excludes — see §6 step 2.

### Rollback

There are no releases or git metadata on the box, so rollback means **re-deploying an older
commit**: check out the good commit locally, build the panel, run the manual deploy above.
(Or `workflow_dispatch` the workflow from the older ref.) Restoring data is separate:
`gunzip` the chosen snapshot from `/var/backups/aizu/` over `/var/lib/aizu/aizu.db` with
the service stopped.

---

## 9. Current state, drift, and open risks

### 🔴 Production is behind `main`

The deployed engine matches commit **`b227528`** ("Add the 9-slide AIZU pitch deck",
2026-08-10) — confirmed by md5-matching `/opt/aizu/engine/aizu/server.py` against git
history. `main` is at **`7fe18f3`** ("Launch", 2026-08-12), which is pushed to `origin`.

So the **"Launch" commit never reached production.** Both `/opt/aizu/panel/index.html` and
`/opt/aizu/engine/` are stamped `2026-08-10 18:18`, and `aizu.uz` still serves
`last-modified: Mon, 10 Aug 2026 13:18:17 GMT`. The service did restart on 2026-08-14, but
that was a host reboot (uptime matches), not a deploy.

The workflow file existed unchanged at `7fe18f3`, so the likely causes are a **failed
`engine`/`panel` job** or a **pending `production` environment approval**. Check the Actions
tab. Symptoms visible in production support the drift: the journal shows repeated
`GET /api/agent/readiness → 404 · unknown endpoint` — the deployed panel calls an endpoint
the deployed engine doesn't have.

### 🟠 Other findings

| Risk | Detail | Suggested fix |
|---|---|---|
| **No deployed-version marker** | No git metadata on the box; "what's running?" requires md5-matching files against history | Have the deploy write `/opt/aizu/VERSION` with `$GITHUB_SHA`, and surface it on a health endpoint |
| **Admin plane is internet-reachable** | IP allowlist opened to `0.0.0.0/0,::/0` on 2026-08-20 by request (§5). The cross-org bypass now rests on TOTP + per-email lockout alone | Narrow to an office/VPN CIDR when practical; if you do, re-check `AIZU_TRUSTED_PROXIES` (§5) |
| **Backups are single-disk** | `/var/backups/aizu` is on the same 38 GB volume as the DB — a disk loss takes both | Ship the nightly `.gz` offsite (S3/R2/rsync target) |
| **Caddy is superseded, and noisy** | Predates the tunnel by 2.5 hours; both its jobs (TLS, IP access) are now Cloudflare's. Still retrying ACME every 6h on attempt 47, already rate-limited down to the staging CA, ~1,073 error lines/week, ~57 MB RSS | Confirm the tunnel target, repoint to `http://localhost:8765` if needed, then `systemctl disable --now caddy` (§4) |
| **Tunnel routing is off-repo** | Public-hostname → service mapping exists only in the Cloudflare dashboard | Document the mapping here, or move to a `config.yml` under version control |
| **`developer` has full sudo** | `(ALL : ALL) ALL` in addition to the scoped NOPASSWD set; anyone with the SSH key is one password away from root | Consider dropping the blanket rule and keeping only the scoped grant |
| **Operator key travelled via Telegram** | The `aka.orihero@gmail.com` key was delivered through a Telegram chat, so a copy sits on Telegram's servers | Generate a fresh pair on the box, swap `authorized_keys`, retire the old key |
| **1.9 GiB RAM** | Fine for the bridge (~50 MB resident), but far too small for CDP/Chrome harvesting | Keep harvest work on the worker fleet, never on this box |
| **Hostname says `abdu-test`** | Production box named like a test box — an easy way to run a destructive command on the wrong machine | `hostnamectl set-hostname aizu-prod` |

---

## 10. See also

- [`../architecture/overview.md`](../architecture/overview.md) — bridge, panel, worker fleet, shared DB
- [`../architecture/api-reference.md`](../architecture/api-reference.md) — the `/api/*` surface behind the tunnel
- [`desktop-packaging.md`](desktop-packaging.md) — packaging the desktop worker
- [`../../CLAUDE.md`](../../CLAUDE.md) — env vars, CLI argument-order gotchas, build commands
