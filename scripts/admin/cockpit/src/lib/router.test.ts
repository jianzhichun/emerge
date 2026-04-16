import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { buildRouteUrl, navigate, readRouteFromUrl } from './router';

describe('router helpers', () => {
  beforeEach(() => {
    vi.stubGlobal('window', {
      location: {
        href: 'http://localhost:8789/',
        origin: 'http://localhost:8789'
      }
    } as unknown as Window);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses overview when tab is missing', () => {
    const route = readRouteFromUrl('http://localhost:8789/?session=s1');
    expect(route).toEqual({
      tab: 'overview',
      session: 's1',
      panel: undefined
    });
  });

  it('trims tab/session/panel query values', () => {
    const route = readRouteFromUrl('http://localhost:8789/?tab=%20monitors%20&session=%20abc%20&panel=%20right%20');
    expect(route).toEqual({
      tab: 'monitors',
      session: 'abc',
      panel: 'right'
    });
  });

  it('sets and unsets session/panel query params', () => {
    const withValues = buildRouteUrl(
      { tab: 'overview', session: 'session-1', panel: 'left' },
      'http://localhost:8789/?tab=overview'
    );
    expect(withValues.searchParams.get('session')).toBe('session-1');
    expect(withValues.searchParams.get('panel')).toBe('left');

    const withoutValues = buildRouteUrl(
      { tab: 'overview', session: '   ', panel: '' },
      withValues
    );
    expect(withoutValues.searchParams.get('session')).toBeNull();
    expect(withoutValues.searchParams.get('panel')).toBeNull();
  });

  it('uses replaceState or pushState based on navigate options', () => {
    const pushState = vi.fn();
    const replaceState = vi.fn();
    vi.stubGlobal('history', {
      state: { existing: true },
      pushState,
      replaceState
    } as unknown as History);

    navigate(
      { tab: 'monitors', session: 'session-2' },
      { replace: true, baseUrl: 'http://localhost:8789/?tab=overview' }
    );
    expect(replaceState).toHaveBeenCalledTimes(1);
    expect(pushState).not.toHaveBeenCalled();

    navigate(
      { tab: 'state' },
      { baseUrl: 'http://localhost:8789/?tab=overview' }
    );
    expect(pushState).toHaveBeenCalledTimes(1);
  });
});
