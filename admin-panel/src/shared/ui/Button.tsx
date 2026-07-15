import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { cn } from '@/shared/lib/cn';

type ButtonVariant = 'default' | 'success' | 'danger' | 'ghost';

const VARIANT_CLASSES: Readonly<Record<ButtonVariant, string>> = {
  default: 'bg-accent text-accent-ink hover:shadow-lift hover:-translate-y-px',
  success: 'bg-success-soft text-success hover:bg-success/25',
  danger: 'bg-danger text-white hover:shadow-lift hover:-translate-y-px',
  ghost: 'border border-border bg-surface text-text shadow-tile hover:shadow-lift hover:-translate-y-px',
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  readonly variant?: ButtonVariant;
  readonly children: ReactNode;
}

export function Button({ variant = 'default', className, children, ...rest }: ButtonProps) {
  return (
    <button
      type="button"
      className={cn(
        'inline-flex items-center justify-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-bold transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-50',
        VARIANT_CLASSES[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
