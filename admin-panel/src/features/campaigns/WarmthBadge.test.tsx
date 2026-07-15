import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { buildCampaign } from '@/test/fixtures';
import { WarmthBadge } from './WarmthBadge';

describe('WarmthBadge', () => {
  test('shows the score and the state label', () => {
    const warmth = { ...buildCampaign().warmth, score: 82, state: 'full' as const };
    render(<WarmthBadge warmth={warmth} />);
    expect(screen.getByText('82%')).toBeInTheDocument();
    expect(screen.getByText('Full volume')).toBeInTheDocument();
  });

  test('labels a low score as Warming', () => {
    const warmth = { ...buildCampaign().warmth, score: 38, state: 'warming' as const };
    render(<WarmthBadge warmth={warmth} />);
    expect(screen.getByText('38%')).toBeInTheDocument();
    expect(screen.getByText('Warming')).toBeInTheDocument();
  });

  test('renders a per-component breakdown in the tooltip', () => {
    const warmth = {
      ...buildCampaign().warmth,
      score: 50,
      components: { age: 0.5, ramp: 0.4, network: 0.2, profile: 1, trust: 0.9 },
    };
    const { container } = render(<WarmthBadge warmth={warmth} />);
    const titled = container.querySelector('[title]');
    expect(titled?.getAttribute('title')).toContain('Age 50%');
    expect(titled?.getAttribute('title')).toContain('Trust 90%');
  });
});
