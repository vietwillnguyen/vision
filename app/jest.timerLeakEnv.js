// Test environment that fails a test file which leaves a live timer behind.
//
// A timer that is still pending and ref'd when a test file finishes keeps its
// jest worker's event loop alive after the run. jest-worker gives a worker
// 500ms to exit before killing it and printing "A worker process has failed to
// exit gracefully and has been force exited... This is likely caused by tests
// leaking due to improper teardown" - and `--detectOpenHandles` cannot point
// at the culprit, because it forces a single in-band run and only reports what
// is still open at the very end of that run. Whether the warning shows up at
// all depends on which worker ran the leaking file last and how long it had
// left, so the leak reads as a flake. This environment instead checks every
// file on its own, at teardown (after every hook of the file has run), and
// fails it with the creation stack of each leaked timer.
//
// Only real timers created through the file's own globals are tracked. A test
// that installs fake timers swaps those globals out, so fake timers are never
// counted; an unref'd timer cannot hold the process open, so it is ignored.
// A timeout due within SHORT_TIMEOUT_MS of teardown is ignored too: it fires
// well inside jest-worker's 500ms exit grace (e.g. react-navigation's 32ms
// tab-animation settle timer), so it cannot cause the forced exit.
const BaseEnvironment = require(require('jest-expo/jest-preset').testEnvironment);

const STACK_FRAMES = 8;
// Half of jest-worker's FORCE_EXIT_DELAY (500ms).
const SHORT_TIMEOUT_MS = 250;

class TimerLeakEnvironment extends BaseEnvironment {
  constructor(config, context) {
    super(config, context);
    this.pendingTimers = new Map();
    this.guardChecked = false;
    this.installTimerTracking();
  }

  installTimerTracking() {
    const g = this.global;
    const pending = this.pendingTimers;
    const realSetTimeout = g.setTimeout;
    const realSetInterval = g.setInterval;
    const realClearTimeout = g.clearTimeout;
    const realClearInterval = g.clearInterval;

    const track = (kind, realSet) =>
      function trackedTimer(callback, delay, ...args) {
        const stack = new Error().stack
          .split('\n')
          .slice(2, 2 + STACK_FRAMES)
          .join('\n');
        let handle;
        const run =
          typeof callback === 'function' && kind === 'setTimeout'
            ? function (...callbackArgs) {
                pending.delete(handle);
                return callback.apply(this, callbackArgs);
              }
            : callback;
        handle = realSet.call(this, run, delay, ...args);
        pending.set(handle, { kind, delay, stack, dueAt: Date.now() + (Number(delay) || 0) });
        return handle;
      };

    const untrack = (realClear) =>
      function trackedClear(handle) {
        if (pending.has(handle)) {
          pending.delete(handle);
        } else if (typeof handle === 'number' || typeof handle === 'string') {
          // Node lets a timer be cleared by its primitive id as well.
          for (const timer of pending.keys()) {
            if (Number(timer) === Number(handle)) pending.delete(timer);
          }
        }
        return realClear.call(this, handle);
      };

    g.setTimeout = track('setTimeout', realSetTimeout);
    g.setInterval = track('setInterval', realSetInterval);
    g.clearTimeout = untrack(realClearTimeout);
    g.clearInterval = untrack(realClearInterval);
  }

  leakedTimers() {
    const now = Date.now();
    return [...this.pendingTimers].filter(
      ([handle, { kind, dueAt }]) =>
        (typeof handle?.hasRef !== 'function' || handle.hasRef()) &&
        (kind === 'setInterval' || dueAt - now > SHORT_TIMEOUT_MS),
    );
  }

  async teardown() {
    // jest-runner calls teardown a second time from a `finally` when the first
    // call throws; report the leak once.
    const leaked = this.guardChecked ? [] : this.leakedTimers();
    this.guardChecked = true;
    // Clear what leaked so this worker can still exit even though the file
    // fails; the failure below is the signal, not the hang.
    for (const [handle, { kind }] of leaked) {
      if (kind === 'setInterval') clearInterval(handle);
      else clearTimeout(handle);
    }
    this.pendingTimers.clear();
    await super.teardown();
    if (leaked.length > 0) {
      const details = leaked
        .map(([, { kind, delay, stack }]) => `${kind}(..., ${delay}) created\n${stack}`)
        .join('\n\n');
      throw new Error(
        `${leaked.length} timer(s) were still pending when this test file finished. ` +
          'A live timer keeps the jest worker from exiting ("A worker process has failed ' +
          'to exit gracefully"); clear it, or close whatever owns it, before the file ends.\n\n' +
          details,
      );
    }
  }
}

module.exports = TimerLeakEnvironment;
