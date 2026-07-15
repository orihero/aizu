import { describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ImageDropzone } from './ImageDropzone';

function fakeFile(name: string, type: string, size: number): File {
  const file = new File(['x'], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

/** The dropzone's hidden file input (no role/label of its own — query the DOM). */
function fileInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (!input) throw new Error('expected a file input');
  return input;
}

describe('ImageDropzone', () => {
  test('reads a picked image to a data URL and calls onChange', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ImageDropzone value={null} onChange={onChange} />);

    const input = fileInput();
    await user.upload(input, new File(['hello'], 'shot.png', { type: 'image/png' }));

    await waitFor(() => { expect(onChange).toHaveBeenCalledTimes(1); });
    expect(onChange.mock.calls[0]?.[0]).toMatch(/^data:image\/png;base64,/);
  });

  test('rejects a non-image with an inline alert and does not call onChange', async () => {
    const onChange = vi.fn();
    render(<ImageDropzone value={null} onChange={onChange} />);

    const input = fileInput();
    // Fire change directly: `accept="image/*"` filters the OS picker, but a drag
    // or a permissive platform can still hand over a non-image — our boundary
    // validation is what must reject it, so we bypass the picker's filter here.
    fireEvent.change(input, { target: { files: [fakeFile('notes.pdf', 'application/pdf', 100)] } });

    expect(await screen.findByRole('alert')).toHaveTextContent(/image/i);
    expect(onChange).not.toHaveBeenCalled();
  });

  test('shows a preview with a distinctly-named remove control when a value is set', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ImageDropzone value="data:image/png;base64,AAAA" onChange={onChange} />);

    // Distinct accessible names: upload vs remove are never the same string.
    expect(screen.queryByLabelText('Upload product screenshot')).toBeNull();
    expect(screen.getByAltText('Product screenshot preview')).toBeInTheDocument();

    await user.click(screen.getByLabelText('Remove screenshot'));
    expect(onChange).toHaveBeenCalledWith(null);
  });
});
