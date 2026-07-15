import { describe, expect, test } from 'vitest';
import { reelUrl } from './reelUrl';

describe('reelUrl', () => {
  test('builds an Instagram reel URL from the shortcode', () => {
    expect(reelUrl('instagram', 'DXOML7vjQhn')).toBe(
      'https://www.instagram.com/reel/DXOML7vjQhn/',
    );
  });

  test('is case-insensitive on the platform name', () => {
    expect(reelUrl('Instagram', 'abc')).toBe('https://www.instagram.com/reel/abc/');
  });

  test('builds a YouTube shorts URL', () => {
    expect(reelUrl('youtube', 'xyz')).toBe('https://www.youtube.com/shorts/xyz');
  });

  test('returns null for platforms without a derivable per-reel URL', () => {
    expect(reelUrl('telegram', 'abc')).toBeNull();
    expect(reelUrl('unknown', 'abc')).toBeNull();
  });

  test('returns null for an empty reel id', () => {
    expect(reelUrl('instagram', '')).toBeNull();
    expect(reelUrl('instagram', '   ')).toBeNull();
  });

  test('encodes ids to keep the URL well-formed', () => {
    expect(reelUrl('instagram', 'a/b?c')).toBe('https://www.instagram.com/reel/a%2Fb%3Fc/');
  });
});
