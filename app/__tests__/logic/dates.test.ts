import { toUtcMidnight, utcDateString, utcRangeEndingAt } from '../../src/logic/dates';

describe('toUtcMidnight', () => {
  it('truncates a mid-day timestamp to 00:00:00.000 UTC', () => {
    const d = new Date('2026-07-18T17:45:12.345Z');
    expect(toUtcMidnight(d).toISOString()).toBe('2026-07-18T00:00:00.000Z');
  });

  it('keeps the UTC calendar day for a local-time west-of-UTC evening', () => {
    // 2026-07-18T23:30-07:00 is 2026-07-19T06:30Z; UTC day is the 19th.
    const d = new Date('2026-07-19T06:30:00.000Z');
    expect(toUtcMidnight(d).toISOString()).toBe('2026-07-19T00:00:00.000Z');
  });
});

describe('utcDateString', () => {
  it('formats as YYYY-MM-DD in UTC', () => {
    expect(utcDateString(new Date('2026-07-18T17:45:12.345Z'))).toBe('2026-07-18');
  });
});

describe('utcRangeEndingAt', () => {
  it('returns a midnight-aligned inclusive range of N days', () => {
    const { start, end } = utcRangeEndingAt(new Date('2026-07-18T17:45:12.345Z'), 30);
    expect(end.toISOString()).toBe('2026-07-18T00:00:00.000Z');
    expect(start.toISOString()).toBe('2026-06-19T00:00:00.000Z');
  });
});
