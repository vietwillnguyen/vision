/**
 * The guard in jest.timerLeakEnv.js is what keeps a leaked timer (the cause of
 * "A worker process has failed to exit gracefully") from coming back as a
 * flake. These tests run a real instance of it, outside the suite's own
 * environment, so a change that quietly disables the check fails here.
 */
// eslint-disable-next-line @typescript-eslint/no-require-imports
const TimerLeakEnvironment = require('../../jest.timerLeakEnv.js');

type TimerGlobals = {
  setTimeout: typeof setTimeout;
  setInterval: typeof setInterval;
  clearTimeout: typeof clearTimeout;
  clearInterval: typeof clearInterval;
};

function makeEnvironment(): { env: { global: TimerGlobals; teardown(): Promise<void> } } {
  const env = new TimerLeakEnvironment(
    {
      globalConfig: {},
      projectConfig: {
        testEnvironmentOptions: {},
        fakeTimers: {},
        moduleFileExtensions: ['js'],
        testPath: __filename,
      },
    },
    { console, docblockPragmas: {}, testPath: __filename },
  );
  return { env };
}

// Handles a test leaks on purpose, cleared afterwards whether or not the
// guard under test cleared them, so a broken guard fails these tests instead
// of hanging the run.
const leaked: ReturnType<typeof setTimeout>[] = [];
afterEach(() => {
  leaked.splice(0).forEach((handle) => {
    clearTimeout(handle);
    clearInterval(handle);
  });
});

describe('jest.timerLeakEnv.js', () => {
  it('fails a file that leaves a long timeout pending, and names where it was created', async () => {
    const { env } = makeEnvironment();
    leaked.push(env.global.setTimeout(() => {}, 10000));

    await expect(env.teardown()).rejects.toThrow(
      /1 timer\(s\) were still pending[\s\S]*setTimeout\(\.\.\., 10000\) created[\s\S]*timerLeakEnv\.test\.ts/,
    );
  });

  it('fails a file that leaves an interval running', async () => {
    const { env } = makeEnvironment();
    leaked.push(env.global.setInterval(() => {}, 50));

    await expect(env.teardown()).rejects.toThrow(/setInterval\(\.\.\., 50\) created/);
  });

  it('passes a file whose timers were cleared, fired, unref-ed, or are about to fire', async () => {
    const { env } = makeEnvironment();
    const g = env.global;
    g.clearTimeout(g.setTimeout(() => {}, 10000));
    g.clearInterval(g.setInterval(() => {}, 10000));
    // Cleared by primitive id, which Node also accepts.
    g.clearTimeout(
      Number(g.setTimeout(() => {}, 10000)) as unknown as ReturnType<typeof setTimeout>,
    );
    g.setTimeout(() => {}, 10000).unref();
    g.setTimeout(() => {}, 30);
    await new Promise<void>((resolve) => g.setTimeout(resolve, 1));

    await expect(env.teardown()).resolves.toBeUndefined();
  });
});
