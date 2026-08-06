module.exports = {
  preset: 'jest-expo',
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?)|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@unimodules/.*|unimodules|sentry-expo|native-base|react-native-svg)',
  ],
  // Default 5000ms trips under full-suite resource contention even though
  // every test passes in isolation - same class of flake already fixed for
  // the live-Supabase suite (see useDeviceStatus.live.test.tsx history).
  testTimeout: 15000,
  // Raising testTimeout alone was not enough: RNTL's waitFor/findBy* have a
  // separate 1000ms budget that jest never sees. jest.setup.js raises that one
  // too, but to a value under this one so RNTL's far better failure message
  // wins the race - see the reasoning there. The jest-expo preset defines no
  // setupFilesAfterEnv, so this adds rather than overrides.
  setupFilesAfterEnv: ['<rootDir>/jest.setup.js'],
};
