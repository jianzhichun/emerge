import { describe, expect, it } from 'vitest';

import { escapeText, formatAge } from './format';

describe('format helpers', () => {
  it('escapes &, <, >, ", and apostrophe characters', () => {
    expect(escapeText(`&<>"'`)).toBe('&amp;&lt;&gt;&quot;&#39;');
  });

  it('formats age boundaries', () => {
    const now = 1_000_000;

    expect(formatAge(undefined, now)).toBe('n/a');
    expect(formatAge(Number.NaN, now)).toBe('n/a');

    expect(formatAge(now, now)).toBe('just now');
    expect(formatAge(now - 4_000, now)).toBe('just now');

    expect(formatAge(now - 5_000, now)).toBe('5s ago');
    expect(formatAge(now - 59_000, now)).toBe('59s ago');

    expect(formatAge(now - 60_000, now)).toBe('1m ago');
    expect(formatAge(now - 3_599_000, now)).toBe('59m ago');

    expect(formatAge(now - 3_600_000, now)).toBe('1h ago');
    expect(formatAge(now - 86_399_000, now)).toBe('23h ago');

    expect(formatAge(now - 86_400_000, now)).toBe('1d ago');
    expect(formatAge(now - 172_800_000, now)).toBe('2d ago');
  });
});
