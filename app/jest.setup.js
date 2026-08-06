// React Native Testing Library's async helpers (`waitFor`, `findBy*`) carry
// their own 1000ms budget, entirely separate from jest's `testTimeout`. That
// budget is wall clock, so a cold-cache run - where Babel is transforming
// modules on the same worker thread the pending promises need to settle on -
// blows it while nothing is actually wrong: the very same tests pass in
// isolation and pass again on a warm cache.
//
// Raise it to match the `testTimeout` in jest.config.js, for the same reason
// recorded there. This is a ceiling, not a delay: a resolved assertion returns
// immediately, so the warm path is unaffected.
const { configure } = require('@testing-library/react-native');

configure({ asyncUtilTimeout: 15000 });
