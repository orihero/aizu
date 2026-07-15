import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { LeadDetails } from './LeadDetails';

describe('LeadDetails', () => {
  test('renders known fields as labeled readable rows', () => {
    render(
      <LeadDetails
        extracted={{
          phone: '+1 415 555 0142',
          email: 'jane@example.com',
          intent: 'pricing',
        }}
      />,
    );
    expect(screen.getByText('Phone')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '+1 415 555 0142' })).toHaveAttribute(
      'href',
      'tel:+14155550142',
    );
    expect(screen.getByText('Email')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'jane@example.com' })).toHaveAttribute(
      'href',
      'mailto:jane@example.com',
    );
    expect(screen.getByText('Asking about pricing')).toBeInTheDocument();
    expect(screen.queryByText(/null/)).not.toBeInTheDocument();
  });

  test('renders missing values as "Not mentioned", never as null/unknown', () => {
    render(
      <LeadDetails extracted={{ phone: null, email: null, intent: 'buy' }} />,
    );
    expect(screen.getAllByText('Not mentioned')).toHaveLength(2);
    expect(screen.getByText('Wants to buy')).toBeInTheDocument();
    expect(screen.queryByText('unknown')).not.toBeInTheDocument();
    expect(screen.queryByText('null')).not.toBeInTheDocument();
  });

  test('renders unknown brief-defined fields generically', () => {
    render(<LeadDetails extracted={{ studio_type: 'crossfit box', reel_author: null }} />);
    expect(screen.getByText('Studio type')).toBeInTheDocument();
    expect(screen.getByText('crossfit box')).toBeInTheDocument();
    expect(screen.getByText('Reel author')).toBeInTheDocument();
    expect(screen.getByText('Not mentioned')).toBeInTheDocument();
  });

  test('shows an empty notice when nothing was extracted', () => {
    render(<LeadDetails extracted={{}} />);
    expect(screen.getByText('No details were extracted.')).toBeInTheDocument();
  });
});
