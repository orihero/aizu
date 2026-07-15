import { describe, expect, test } from 'vitest';
import { renderHook } from '@testing-library/react';
import { MAX_IMAGE_BYTES, useImageUpload, validateImageFile } from './useImageUpload';

/** Build a File whose `.type` and `.size` are controllable for the validators. */
function fakeFile(name: string, type: string, size: number): File {
  const file = new File(['x'], name, { type });
  // jsdom derives size from the blob parts; override it for the cap test.
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

describe('validateImageFile', () => {
  test('rejects a non-image file with a clear message', () => {
    const result = validateImageFile(fakeFile('notes.pdf', 'application/pdf', 100));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.message).toMatch(/image/i);
  });

  test('rejects a file over the size cap before reading', () => {
    const result = validateImageFile(fakeFile('huge.png', 'image/png', MAX_IMAGE_BYTES + 1));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.message).toMatch(/too large|MB/i);
  });

  test('accepts an in-bounds image', () => {
    const result = validateImageFile(fakeFile('shot.png', 'image/png', 1024));
    expect(result.ok).toBe(true);
  });
});

describe('useImageUpload.readImage', () => {
  test('returns a data URL for a valid image', async () => {
    const { result } = renderHook(() => useImageUpload());
    const file = new File(['hello'], 'shot.png', { type: 'image/png' });
    const read = await result.current.readImage(file);
    expect(read.ok).toBe(true);
    if (read.ok) expect(read.value).toMatch(/^data:image\/png;base64,/);
  });

  test('returns a validation error for a non-image without reading', async () => {
    const { result } = renderHook(() => useImageUpload());
    const read = await result.current.readImage(fakeFile('x.txt', 'text/plain', 10));
    expect(read.ok).toBe(false);
    if (!read.ok) expect(read.error.kind).toBe('validation');
  });
});
