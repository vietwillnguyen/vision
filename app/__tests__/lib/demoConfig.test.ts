import { parseDemoConfig } from '../../src/lib/demoConfig';

describe('parseDemoConfig', () => {
  it('returns the demo sign-in when both values are set', () => {
    expect(parseDemoConfig(' demo@example.com ', 'pw')).toEqual({
      email: 'demo@example.com',
      password: 'pw',
    });
  });

  it.each([
    [undefined, undefined],
    ['demo@example.com', undefined],
    [undefined, 'pw'],
    ['  ', 'pw'],
    ['demo@example.com', ''],
  ])('is absent when the build has no complete demo config (%p, %p)', (email, password) => {
    expect(parseDemoConfig(email, password)).toBeNull();
  });
});
