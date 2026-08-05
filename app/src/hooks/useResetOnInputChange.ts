import { useState } from 'react';

/**
 * Reset derived state when a hook's inputs change, during render rather than
 * from inside an effect.
 *
 * Every data hook here follows the same shape: an effect that resets state to
 * `loading` and then fetches. Doing that reset synchronously in the effect body
 * renders the stale value once before the reset lands, which is what
 * `react-hooks/set-state-in-effect` flags. React's documented alternative is to
 * compare the previous inputs during render and adjust then, so React discards
 * the in-progress render and re-runs before anything is shown:
 * https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
 *
 * `reset` is not called on the first render - the `useState` initializer in each
 * hook already establishes the initial value, so the effect's reset was only
 * ever load-bearing when the inputs changed on a later render.
 */
export function useResetOnInputChange(inputs: readonly unknown[], reset: () => void): void {
  const [previous, setPrevious] = useState(inputs);

  const changed =
    previous.length !== inputs.length ||
    inputs.some((input, index) => !Object.is(input, previous[index]));

  if (changed) {
    setPrevious(inputs);
    reset();
  }
}
