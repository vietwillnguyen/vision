// Load react-native's lazily-exported components while the test file is being
// set up, not inside the first test that renders them.
//
// `react-native`'s index exports every component through a lazy getter, so
// `import { Text, Button } from 'react-native'` does not load `Text` until a
// render first touches it. That first touch pulls in the renderer
// (`ReactFabric-dev.js` alone is ~1.5s of Babel on a cold cache), Animated,
// and Pressability: ~90 modules, ~3.6s of transform for one worker on an idle
// machine. On a cold cache (every CI run) that cost landed inside the first
// rendering test of each suite and counted against its timeout and RNTL's
// `waitFor` budget; with every worker transforming the same files at once on a
// loaded host it reached 20s+ and suites timed out.
//
// The same applies one level down: the renderer reaches react-native through
// `ReactNativePrivateInterface`, whose members are lazy getters too. The first
// host instance a test creates pulls `ReactFabricPublicInstance` and the DOM
// web-API shims behind it (~25 modules, another ~2.3s cold), so every member
// is touched here as well.
//
// Setup files run before the test clock starts and have no timeout, so paying
// the cost here leaves each test timing only its own work. The list covers the
// components the app renders; anything missing only costs speed, not
// correctness, because most components share the heavy modules above.
const RN = require('react-native');

for (const name of [
  'ActivityIndicator',
  'Alert',
  'Animated',
  'Button',
  'FlatList',
  'I18nManager',
  'Image',
  'Pressable',
  'ScrollView',
  'Share',
  'StatusBar',
  'Text',
  'TextInput',
  'TouchableOpacity',
  'View',
  'useColorScheme',
]) {
  if (RN[name] === undefined) {
    throw new Error(`jest.preload.js: react-native has no export named ${name}`);
  }
}
// Animated's own component wrappers are a further lazy layer.
void RN.Animated.View;

// Deep import: this is the renderer's own entry point into react-native, the
// same one react-native's jest preset mocks against. Fail loudly if a future
// react-native moves it rather than silently losing the preload.
const rendererInterface = require('react-native/Libraries/ReactPrivate/ReactNativePrivateInterface');
for (const name of Object.keys(rendererInterface)) {
  void rendererInterface[name];
}
