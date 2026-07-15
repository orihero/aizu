import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { CampaignGenerateProgress } from './CampaignGenerateProgress';

describe('CampaignGenerateProgress', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  test('advances through the stages and CLAMPS at the last one (never loops)', () => {
    render(<CampaignGenerateProgress onCancel={() => {}} />);
    const status = screen.getByRole('status');

    expect(status).toHaveTextContent(/reading your link/i);

    act(() => { vi.advanceTimersByTime(1600); });
    expect(status).toHaveTextContent(/analyzing your product/i);

    act(() => { vi.advanceTimersByTime(1600); });
    expect(status).toHaveTextContent(/drafting your campaign/i);

    // Far past the last interval — it must STAY on the final stage, not wrap back
    // to the first (a looping indicator reads as "stuck").
    act(() => { vi.advanceTimersByTime(1600 * 5); });
    expect(status).toHaveTextContent(/drafting your campaign/i);
  });

  test('the status region is announced politely', () => {
    render(<CampaignGenerateProgress onCancel={() => {}} />);
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite');
  });
});
