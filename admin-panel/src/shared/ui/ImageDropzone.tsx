import { useRef, useState, type DragEvent } from 'react';
import { UploadCloud, X } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import { useImageUpload } from '@/shared/hooks/useImageUpload';

interface ImageDropzoneProps {
  /** The current image as a base64 data URL, or null when none is chosen. */
  readonly value: string | null;
  /** Called with the read data URL on a successful pick, or null on remove. */
  readonly onChange: (dataUrl: string | null) => void;
}

/**
 * Accessible product-screenshot picker: a real button opens a hidden file input,
 * drag-and-drop is supported with a brand-tint hover state, and the file is
 * validated (image + size cap) and base64-encoded via useImageUpload. Errors are
 * announced inline rather than thrown.
 */
export function ImageDropzone({ value, onChange }: ImageDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const { readImage } = useImageUpload();
  const [isDragging, setIsDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleFile(file: File | undefined) {
    if (!file) return;
    setError(null);
    const result = await readImage(file);
    if (result.ok) {
      onChange(result.value);
    } else {
      // A validation/read failure is shown in place; the previous value (if any) stays.
      setError(result.error.message);
    }
  }

  function onDrop(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setIsDragging(false);
    void handleFile(event.dataTransfer.files[0]);
  }

  function onDragOver(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setIsDragging(true);
  }

  function onDragLeave(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setIsDragging(false);
  }

  if (value) {
    return (
      <div className="relative overflow-hidden rounded-tile border border-border bg-surface">
        <img src={value} alt="Product screenshot preview" className="max-h-48 w-full object-contain" />
        <button
          type="button"
          aria-label="Remove screenshot"
          onClick={() => { onChange(null); setError(null); }}
          className="absolute right-2 top-2 inline-flex size-7 items-center justify-center rounded-full border border-border bg-surface text-text-muted shadow-tile transition hover:text-text active:scale-95"
        >
          <X className="size-4" aria-hidden />
        </button>
      </div>
    );
  }

  return (
    <div>
      <button
        type="button"
        aria-label="Upload product screenshot"
        onClick={() => inputRef.current?.click()}
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        className={cn(
          'flex w-full flex-col items-center justify-center gap-2 rounded-tile border-2 border-dashed px-4 py-8 text-center transition',
          isDragging
            ? 'border-brand bg-brand/10 text-text'
            : 'border-border bg-surface text-text-muted hover:border-brand hover:text-text',
        )}
      >
        <UploadCloud className="size-6 text-text-faint" aria-hidden />
        <span className="text-[13px] font-bold">Drop a product screenshot, or click to upload</span>
        <span className="text-[11px] text-text-faint">PNG or JPG, up to 4 MB</span>
      </button>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(e) => { void handleFile(e.target.files?.[0]); e.target.value = ''; }}
      />
      {error ? (
        <p role="alert" className="mt-2 text-[11px] font-semibold text-danger">{error}</p>
      ) : null}
    </div>
  );
}
