import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { useAuth } from '@/shared/hooks/useAuth';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { ROLE_LABELS } from '@/shared/auth/roles';
import type { InviteInfo } from '@/shared/types/domain';
import { AuthForm } from './AuthForm';

const LOGIN_FOOTER = (
  <>
    Already have an account?{' '}
    <Link to="/login" className="font-semibold text-brand hover:underline">
      Log in
    </Link>
  </>
);

export function SignupPage() {
  const { signup } = useAuth();
  const repository = usePanelRepository();
  const [params] = useSearchParams();
  const inviteToken = params.get('invite');
  const [invite, setInvite] = useState<InviteInfo | null>(null);
  const [checking, setChecking] = useState(Boolean(inviteToken));

  useEffect(() => {
    if (!inviteToken) return;
    let cancelled = false;
    void repository.getInvite(inviteToken).then((result) => {
      if (cancelled) return;
      setInvite(result.ok ? result.value : null);
      setChecking(false);
    });
    return () => {
      cancelled = true;
    };
  }, [inviteToken, repository]);

  // --- Invite flow: resolve the token, then join the existing company ---
  if (inviteToken) {
    if (checking) {
      return (
        <div role="status" aria-label="Checking invite" className="flex justify-center py-10">
          <Loader2 className="size-6 animate-spin text-text-faint" aria-hidden />
        </div>
      );
    }
    if (!invite) {
      return (
        <div className="flex flex-col gap-3">
          <h1 className="font-head text-xl font-extrabold tracking-tight text-text">
            Invite not valid
          </h1>
          <p className="text-[13px] text-text-muted">
            This invite link is invalid or has expired. Ask whoever invited you for a fresh
            link, or{' '}
            <Link to="/signup" className="font-semibold text-brand hover:underline">
              create your own company
            </Link>
            .
          </p>
        </div>
      );
    }
    const orgName = invite.orgName ?? 'the team';
    return (
      <AuthForm
        mode="signup"
        heading={`Join ${orgName}`}
        subheading={`You've been invited as ${ROLE_LABELS[invite.role]}. Set a password to join.`}
        submitLabel="Join"
        {...(invite.email ? { fixedEmail: invite.email } : {})}
        onSubmit={(values) =>
          signup({ email: values.email, password: values.password, inviteToken })
        }
        footer={LOGIN_FOOTER}
      />
    );
  }

  // --- Self-serve: create a new company (signer becomes its owner) ---
  return (
    <AuthForm
      mode="signup"
      heading="Create your account"
      subheading="Set up your company and start tracking leads with AIZU."
      submitLabel="Create account"
      showCompanyFields
      onSubmit={(values) =>
        signup({
          email: values.email,
          password: values.password,
          ...(values.companyName ? { companyName: values.companyName } : {}),
          ...(values.companyLogo ? { companyLogo: values.companyLogo } : {}),
          ...(values.companyDescription ? { companyDescription: values.companyDescription } : {}),
        })
      }
      footer={LOGIN_FOOTER}
    />
  );
}
