module.exports = {
  preset: 'jest-expo',
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?)|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@unimodules/.*|unimodules|sentry-expo|native-base|react-native-svg)',
  ],
  // Default 5000ms trips under full-suite resource contention even though
  // every test passes in isolation - same class of flake already fixed for
  // the live-Supabase suite (see useDeviceStatus.live.test.tsx history).
  testTimeout: 15000,
};
