module.exports = {
  preset: 'jest-expo',
  // The preset's environment plus a per-file check that no live timer
  // outlives the file; see the file for why.
  testEnvironment: '<rootDir>/jest.timerLeakEnv.js',
  // Appended after the preset's own setupFiles; see the file for why.
  setupFiles: ['<rootDir>/jest.preload.js'],
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
  testTimeout: 15000,
  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'],
};
