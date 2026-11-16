import { applyWebColorScheme } from '../../src/lib/webColorScheme';

describe('applyWebColorScheme', () => {
  it('marks the document dark so browser-drawn scrollbars match the theme', () => {
    const root = { style: { colorScheme: '' } };
    applyWebColorScheme({ documentElement: root });
    expect(root.style.colorScheme).toBe('dark');
  });

  it('is a no-op without a document (native, SSR)', () => {
    expect(() => applyWebColorScheme(undefined)).not.toThrow();
  });
});
