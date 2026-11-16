// `testTimeout` in jest.config.js does not cover `waitFor`: React Native
// Testing Library has its own 1000ms `asyncUtilTimeout`, and the first
// `waitFor` in a file has to outlast that file's cold Babel transform. CI runs
// with an empty cache every time, so the default made the first assertion of a
// suite fail intermittently (reproducible with `npx jest --clearCache`).
const { configure } = require('@testing-library/react-native');

// 10s, not the default 1s: a cold first render measured ~4s here, and
// jest.config.js's 15s testTimeout still bounds a `waitFor` that never settles.
configure({ asyncUtilTimeout: 10000 });
