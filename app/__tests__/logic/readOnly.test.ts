import { formatDayLabel, isReadOnlyUser, middayOf } from '../../src/logic/readOnly';

describe('isReadOnlyUser', () => {
  it('is true only for an explicit read_only: true in app_metadata', () => {
    expect(isReadOnlyUser({ app_metadata: { read_only: true } })).toBe(true);
    expect(isReadOnlyUser({ app_metadata: { read_only: 'true' } })).toBe(false);
    expect(isReadOnlyUser({ app_metadata: {} })).toBe(false);
    expect(isReadOnlyUser({})).toBe(false);
    expect(isReadOnlyUser(null)).toBe(false);
  });
});

describe('day helpers', () => {
  it('anchors a reel date at UTC midday so every UTC helper lands on that date', () => {
    expect(middayOf('2026-11-20').toISOString()).toBe('2026-11-20T12:00:00.000Z');
  });

  it('labels a UTC date for a heading', () => {
    expect(formatDayLabel('2026-11-20')).toBe('Fri 20 Nov 2026');
  });
});
