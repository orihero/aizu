import { z } from 'zod';

/**
 * Boundary schemas for the bridge's auth + org endpoints. Every auth payload
 * entering the app is validated here before it crosses in — the same
 * never-trust-external-JSON rule the panel-state schemas follow.
 */

/** The four RBAC roles. Mirror of engine/reelradar/rbac.py + shared/auth/roles.ts. */
export const roleSchema = z.enum(['owner', 'admin', 'member', 'viewer']);

/** The company a user belongs to (null while a session has no org context). */
export const orgSchema = z
  .object({
    id: z.number().nullable().catch(null),
    name: z.string().nullable().catch(null),
    logo: z.string().nullable().catch(null),
    description: z.string().nullable().catch(null),
  })
  .nullable();

export const authUserSchema = z.object({
  // Nullable: an org-level superadmin impersonation has no specific user id (it acts as
  // the org, not a person). A real signed-in user always has a numeric id.
  id: z.number().nullable().catch(null),
  // Validate the format at the boundary — a malformed email from the server is
  // a shape mismatch, not something to silently store and render.
  email: z.string().email(),
  // RBAC context. Fail-safe to least privilege if the server ever omits/garbles it.
  role: roleSchema.catch('viewer'),
  orgId: z.number().nullable().catch(null),
  org: orgSchema.catch(null),
  // True when this session is a superadmin actively impersonating a tenant — the org app
  // shows a banner + exit. Absent/false for a real user. Additive → tolerant default.
  impersonated: z.boolean().optional().catch(false),
});

/**
 * The shared {ok,data,error} envelope for /api/auth/{signup,login} and /api/auth/me.
 * `data.user` is present on success; on failure (incl. 401 from /me) data is null.
 */
export const authResponseSchema = z.object({
  ok: z.boolean(),
  data: z.object({ user: authUserSchema }).nullable(),
  error: z.string().nullable(),
});

/** Public invite-landing lookup (GET /api/invite?token=…). */
export const inviteLookupSchema = z.object({
  orgName: z.string().nullable().catch(null),
  orgLogo: z.string().nullable().catch(null),
  email: z.string().nullable().catch(null),
  role: roleSchema,
  valid: z.boolean(),
});

export const inviteLookupResponseSchema = z.object({
  ok: z.boolean(),
  data: inviteLookupSchema.nullable(),
  error: z.string().nullable(),
});

/** POST /api/invite (create) response — the shareable link is returned once. */
export const inviteLinkSchema = z.object({
  token: z.string(),
  path: z.string(),
  role: roleSchema,
  email: z.string().nullable().catch(null),
});

export const inviteLinkResponseSchema = z.object({
  ok: z.boolean(),
  data: inviteLinkSchema.nullable(),
  error: z.string().nullable(),
});
