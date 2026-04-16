const ESCAPE_LOOKUP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
};

export function escapeText(value: unknown): string {
  const text = String(value ?? '');
  return text.replace(/[&<>"']/g, (char) => ESCAPE_LOOKUP[char] ?? char);
}

export function formatAge(tsMs: number | null | undefined, nowMs = Date.now()): string {
  if (!Number.isFinite(tsMs)) {
    return 'n/a';
  }
  const diff = Math.max(0, nowMs - Number(tsMs));
  const sec = Math.floor(diff / 1000);
  if (sec < 5) {
    return 'just now';
  }
  if (sec < 60) {
    return `${sec}s ago`;
  }
  const min = Math.floor(sec / 60);
  if (min < 60) {
    return `${min}m ago`;
  }
  const hour = Math.floor(min / 60);
  if (hour < 24) {
    return `${hour}h ago`;
  }
  const day = Math.floor(hour / 24);
  return `${day}d ago`;
}
