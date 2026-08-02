// Flat config, per the Expo SDK 53+ guidance for the pinned SDK 57
// (https://docs.expo.dev/guides/using-eslint/). eslint-config-expo bundles the
// React, React Hooks, import, and @typescript-eslint plugins, so none of those
// are listed as direct dependencies.
const { defineConfig, globalIgnores } = require('eslint/config');
const expoConfig = require('eslint-config-expo/flat');

module.exports = defineConfig([
  globalIgnores(['dist/*', 'node_modules/*', '.expo/*']),
  expoConfig,
  {
    // Every console call in this app is a deliberate warn/error diagnostic in a
    // hook's failure path; allowing exactly those keeps stray debug logging out
    // without annotating the intentional ones.
    rules: {
      'no-console': ['warn', { allow: ['warn', 'error'] }],
    },
  },
]);
