import { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import { AUTH_INPUT_CLASS, AUTH_INPUT_ERROR_CLASS } from './authStyles';

interface PasswordFieldProps {
  readonly id: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly autoComplete: string;
  readonly error?: string;
  readonly hint?: string;
  readonly disabled?: boolean;
}

/** Password input with a show/hide toggle (the toggle carries its own a11y label). */
export function PasswordField({
  id,
  value,
  onChange,
  autoComplete,
  error,
  hint,
  disabled,
}: PasswordFieldProps) {
  const [visible, setVisible] = useState(false);
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-xs font-semibold text-text-muted">
        Password
      </label>
      <div className="relative">
        <input
          id={id}
          type={visible ? 'text' : 'password'}
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
          }}
          autoComplete={autoComplete}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          className={cn(AUTH_INPUT_CLASS, 'pr-10', error && AUTH_INPUT_ERROR_CLASS)}
        />
        <button
          type="button"
          onClick={() => {
            setVisible((current) => !current);
          }}
          aria-label={visible ? 'Hide password' : 'Show password'}
          className="absolute inset-y-0 right-0 flex items-center px-3 text-text-faint transition-colors hover:text-text"
        >
          {visible ? <EyeOff className="size-4" aria-hidden /> : <Eye className="size-4" aria-hidden />}
        </button>
      </div>
      {error ? (
        <p id={`${id}-error`} className="text-[11px] font-medium text-danger">
          {error}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-[11px] text-text-faint">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
