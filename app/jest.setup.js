// React Native Testing Library's async helpers (`waitFor`, `findBy*`) carry
// their own 1000ms budget, entirely separate from jest's `testTimeout`. That
// budget is wall clock, so a cold-cache run - where Babel is transforming
// modules on the same worker thread the pending promises need to settle on -
// blows it while nothing is actually wrong: the very same tests pass in
// isolation and pass again on a warm cache.
//
// Raise it for the same reason recorded against `testTimeout` in
// jest.config.js, but deliberately *below* that 15000ms rather than equal to
// it. The two clocks start at different moments: jest's runs from the start of
// the test, RNTL's only from the `waitFor`/`findBy*` call. On an equal budget
// jest's therefore always expires first, and a genuinely broken test reports a
// bare "Exceeded timeout of 15000 ms" instead of RNTL's "Unable to find an
// element with..." plus the rendered tree. Leaving headroom keeps that
// diagnostic reachable while still clearing cold-cache transform contention.
//
// This is a ceiling, not a delay: a resolved assertion returns immediately, so
// the warm path is unaffected.
const { configure } = require('@testing-library/react-native');

configure({ asyncUtilTimeout: 10000 });
