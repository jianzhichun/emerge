export interface CockpitRouteState {
  tab: string;
  session?: string;
  panel?: string;
}

export interface NavigateOptions {
  replace?: boolean;
  baseUrl?: string;
}

export const DEFAULT_TAB = 'overview';

function clean(value: string | null): string | undefined {
  const next = (value ?? '').trim();
  return next ? next : undefined;
}

export function readRouteFromUrl(url: string | URL = window.location.href): CockpitRouteState {
  const parsed = typeof url === 'string' ? new URL(url, window.location.origin) : url;
  return {
    tab: clean(parsed.searchParams.get('tab')) ?? DEFAULT_TAB,
    session: clean(parsed.searchParams.get('session')),
    panel: clean(parsed.searchParams.get('panel'))
  };
}

export function buildRouteUrl(
  route: Partial<CockpitRouteState>,
  baseUrl: string | URL = window.location.href
): URL {
  const url = typeof baseUrl === 'string' ? new URL(baseUrl, window.location.origin) : new URL(baseUrl.toString());
  url.searchParams.set('tab', clean(route.tab ?? null) ?? DEFAULT_TAB);

  const session = clean(route.session ?? null);
  const panel = clean(route.panel ?? null);
  if (session) {
    url.searchParams.set('session', session);
  } else {
    url.searchParams.delete('session');
  }
  if (panel) {
    url.searchParams.set('panel', panel);
  } else {
    url.searchParams.delete('panel');
  }
  return url;
}

export function navigate(route: Partial<CockpitRouteState>, options: NavigateOptions = {}): CockpitRouteState {
  const url = buildRouteUrl(route, options.baseUrl ?? window.location.href);
  const next = readRouteFromUrl(url);
  const statePayload = { ...history.state, cockpit: next };
  if (options.replace) {
    history.replaceState(statePayload, '', url);
  } else {
    history.pushState(statePayload, '', url);
  }
  return next;
}

export function applyFromUrl(apply: (route: CockpitRouteState) => void, url?: string | URL): CockpitRouteState {
  const route = readRouteFromUrl(url ?? window.location.href);
  apply(route);
  return route;
}
