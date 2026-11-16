module.exports = {
  preset: 'jest-expo',
  // node-fetch 3 and its three runtime deps ship ESM only; jest-expo runs
  // CommonJS, so they must go through babel like the react-native packages.
  // node-fetch is only the live Supabase test's real-fetch escape hatch (see
  // useDeviceStatus.live.test.tsx).
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?)|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@unimodules/.*|unimodules|sentry-expo|native-base|react-native-svg|node-fetch|data-uri-to-buffer|fetch-blob|formdata-polyfill)',
  ],
  // Default 5000ms trips under full-suite resource contention even though
  // every test passes in isolation - same class of flake already fixed for
  // the live-Supabase suite (see useDeviceStatus.live.test.tsx history).
  // 15000 was still too tight: the slowest test measures ~13.0s on a cold
  // cache, so it cleared by under two seconds here and would not on a slower
  // runner. Must stay above jest.setup.js's asyncUtilTimeout.
  testTimeout: 45000,
  // Raising testTimeout alone was not enough: RNTL's waitFor/findBy* have a
  // separate 1000ms budget that jest never sees. jest.setup.js raises that one
  // too, but to a value under this one so RNTL's far better failure message
  // wins the race - see the reasoning there. The jest-expo preset defines no
  // setupFilesAfterEnv, so this adds rather than overrides.
  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'],
};
