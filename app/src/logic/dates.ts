export function toUtcMidnight(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

export function utcDateString(d: Date): string {
  return toUtcMidnight(d).toISOString().slice(0, 10);
}

export function utcRangeEndingAt(end: Date, days: number): { start: Date; end: Date } {
  const endMidnight = toUtcMidnight(end);
  const start = new Date(endMidnight);
  start.setUTCDate(start.getUTCDate() - (days - 1));
  return { start, end: endMidnight };
}
