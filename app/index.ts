import { registerRootComponent } from 'expo';

import App from './App';
import { applyWebColorScheme } from './src/lib/webColorScheme';

applyWebColorScheme(typeof document === 'undefined' ? undefined : document);

// registerRootComponent calls AppRegistry.registerComponent('main', () => App);
// It also ensures that whether you load the app in Expo Go or in a native build,
// the environment is set up appropriately
registerRootComponent(App);
