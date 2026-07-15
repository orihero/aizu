import { describe, expect, test } from 'vitest';
import { platformLabel } from './platformLabel';

describe('platformLabel', () => {
  test('uppercases the single-letter X brand', () => {
    expect(platformLabel('x')).toBe('X');
  });

  test('camel-cases LinkedIn and YouTube correctly', () => {
    expect(platformLabel('linkedin')).toBe('LinkedIn');
    expect(platformLabel('youtube')).toBe('YouTube');
  });

  test('title-cases the simple platforms', () => {
    expect(platformLabel('instagram')).toBe('Instagram');
    expect(platformLabel('telegram')).toBe('Telegram');
    expect(platformLabel('reddit')).toBe('Reddit');
  });

  test('is case-insensitive on the key', () => {
    expect(platformLabel('LinkedIn')).toBe('LinkedIn');
    expect(platformLabel('X')).toBe('X');
  });

  test('falls back to capitalizing an unknown platform', () => {
    expect(platformLabel('mastodon')).toBe('Mastodon');
  });
});
