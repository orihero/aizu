import { useState } from 'react';
import {
  Check,
  Copy,
  Mail,
  Phone,
  Tag,
  type LucideIcon,
} from 'lucide-react';
import { Badge } from './Badge';

/**
 * Human-readable rendering of a match's brief-defined `extracted` blob
 * (CRM-style labeled rows instead of raw JSON). Common cross-domain contact
 * fields get icons and friendly wording; any other brief's fields fall back to
 * a generic prettified row, so the panel stays brief-agnostic.
 */

const KNOWN_FIELDS = ['phone', 'email', 'intent'] as const;

const FIELD_META: Readonly<Record<string, { label: string; icon: LucideIcon }>> = {
  phone: { label: 'Phone', icon: Phone },
  email: { label: 'Email', icon: Mail },
  intent: { label: 'Intent', icon: Tag },
};

const INTENT_LABELS: Readonly<Record<string, string>> = {
  buy: 'Wants to buy',
  subscribe: 'Wants to subscribe',
  trial: 'Wants a free trial',
  demo: 'Wants a demo',
  pricing: 'Asking about pricing',
  inquire: 'Asking questions',
};

const COPY_FEEDBACK_MS = 1500;

function asDisplayValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed === '' || trimmed.toLowerCase() === 'unknown' ? null : trimmed;
  }
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return null; // nested structures are not displayable as a lead fact
}

function prettifyKey(key: string): string {
  const spaced = key.replace(/[_-]+/g, ' ');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function CopyButton({ value }: { readonly value: string }) {
  const [isCopied, setIsCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setIsCopied(true);
      setTimeout(() => {
        setIsCopied(false);
      }, COPY_FEEDBACK_MS);
    } catch {
      // clipboard unavailable (permissions/insecure context) — nothing to do
    }
  };
  return (
    <button
      type="button"
      onClick={() => void copy()}
      aria-label={`Copy ${value}`}
      className="rounded-full p-1 text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
    >
      {isCopied ? (
        <Check className="size-3 text-success" aria-hidden />
      ) : (
        <Copy className="size-3" aria-hidden />
      )}
    </button>
  );
}

function FieldRow({
  icon: Icon,
  label,
  children,
}: {
  readonly icon: LucideIcon;
  readonly label: string;
  readonly children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2.5 py-2.5">
      <Icon className="size-3.5 shrink-0 text-text-faint" aria-hidden />
      <span className="w-28 shrink-0 text-[10px] font-semibold uppercase tracking-wide text-text-faint">
        {label}
      </span>
      <span className="min-w-0 grow text-right text-xs font-medium text-text">{children}</span>
    </div>
  );
}

function NotMentioned() {
  return <span className="italic text-text-faint">Not mentioned</span>;
}

function PhoneValue({ phone }: { readonly phone: string }) {
  return (
    <span className="flex items-center justify-end gap-1">
      <a
        href={`tel:${phone.replace(/\s+/g, '')}`}
        className="font-mono font-medium text-brand hover:underline"
      >
        {phone}
      </a>
      <CopyButton value={phone} />
    </span>
  );
}

function EmailValue({ email }: { readonly email: string }) {
  return (
    <span className="flex items-center justify-end gap-1">
      <a href={`mailto:${email}`} className="font-mono font-medium text-brand hover:underline">
        {email}
      </a>
      <CopyButton value={email} />
    </span>
  );
}

function knownFieldValue(field: string, value: string): React.ReactNode {
  if (field === 'phone') return <PhoneValue phone={value} />;
  if (field === 'email') return <EmailValue email={value} />;
  if (field === 'intent') {
    return <Badge tone="success">{INTENT_LABELS[value.toLowerCase()] ?? prettifyKey(value)}</Badge>;
  }
  return value;
}

interface LeadDetailsProps {
  readonly extracted: Readonly<Record<string, unknown>>;
}

export function LeadDetails({ extracted }: LeadDetailsProps) {
  const extraKeys = Object.keys(extracted).filter(
    (key) => !KNOWN_FIELDS.includes(key as (typeof KNOWN_FIELDS)[number]),
  );
  const knownPresent = KNOWN_FIELDS.filter((field) => field in extracted);

  if (knownPresent.length === 0 && extraKeys.length === 0) {
    return <p className="text-xs italic text-text-faint">No details were extracted.</p>;
  }

  return (
    <div className="divide-y divide-border rounded-card border border-border bg-surface px-4 py-1 shadow-tile">
      {knownPresent.map((field) => {
        const meta = FIELD_META[field];
        if (!meta) return null;
        const value = asDisplayValue(extracted[field]);
        return (
          <FieldRow key={field} icon={meta.icon} label={meta.label}>
            {value === null ? <NotMentioned /> : knownFieldValue(field, value)}
          </FieldRow>
        );
      })}
      {extraKeys.map((key) => {
        const value = asDisplayValue(extracted[key]);
        return (
          <FieldRow key={key} icon={Tag} label={prettifyKey(key)}>
            {value === null ? <NotMentioned /> : value}
          </FieldRow>
        );
      })}
    </div>
  );
}
