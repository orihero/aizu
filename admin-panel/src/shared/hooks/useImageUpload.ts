import { useCallback } from 'react';
import { appError, err, ok, type Result } from '@/shared/lib/result';

/** ~4 MB upload cap, enforced BEFORE reading so we never base64 a huge file. */
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

const MAX_IMAGE_MB = Math.round(MAX_IMAGE_BYTES / (1024 * 1024));

/**
 * Pure, synchronous validation of a picked file at the boundary (don't trust the
 * OS file picker): must be an image and within the size cap. Returns a typed
 * Result so the caller never has to catch.
 */
export function validateImageFile(file: File): Result<File> {
  if (!file.type.startsWith('image/')) {
    return err(appError('validation', 'Please choose an image file (PNG or JPG).'));
  }
  if (file.size > MAX_IMAGE_BYTES) {
    return err(appError('validation', `That image is too large — keep it under ${MAX_IMAGE_MB} MB.`));
  }
  return ok(file);
}

/** Reads a validated image as a `data:` base64 URL, never throwing. */
function readAsDataUrl(file: File): Promise<Result<string>> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result === 'string') {
        resolve(ok(result));
      } else {
        resolve(err(appError('unknown', 'Could not read the image.')));
      }
    };
    reader.onerror = () => {
      resolve(err(appError('unknown', 'Could not read the image.')));
    };
    reader.readAsDataURL(file);
  });
}

export interface UseImageUpload {
  /** Validate then read a picked file to a base64 data URL (never throws). */
  readonly readImage: (file: File) => Promise<Result<string>>;
}

/**
 * Encapsulates file→base64 conversion + validation behind a Result-returning
 * function, so components handle a value rather than catch a FileReader error.
 */
export function useImageUpload(): UseImageUpload {
  const readImage = useCallback(async (file: File): Promise<Result<string>> => {
    const valid = validateImageFile(file);
    if (!valid.ok) return valid;
    return readAsDataUrl(valid.value);
  }, []);

  return { readImage };
}
