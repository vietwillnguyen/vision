import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { NavigationContainer, DarkTheme } from '@react-navigation/native';
import type { Session } from '@supabase/supabase-js';
import { StatusBar } from 'expo-status-bar';
import React from 'react';
import { Platform, StyleSheet, Text, View } from 'react-native';
import {
  initialWindowMetrics,
  SafeAreaProvider,
  useSafeAreaInsets,
} from 'react-native-safe-area-context';

import { ArchiveContainer } from './src/containers/ArchiveContainer';
import { AuthContainer } from './src/containers/AuthContainer';
import { DemoBanner } from './src/components/DemoBanner';
import { ArchiveIcon, DeviceIcon, RawFootageIcon, TodayReelIcon } from './src/components/TabIcons';
import { WebFrame } from './src/components/WebFrame';
import { DeviceStack } from './src/navigation/DeviceStack';
import { RawFootageContainer } from './src/containers/RawFootageContainer';
import { TodayReelContainer } from './src/containers/TodayReelContainer';
import { useAuth } from './src/hooks/useAuth';
import { useDevice } from './src/hooks/useDevice';
import { useLatestReelDate } from './src/hooks/useReel';
import { demoConfig as buildDemoConfig, type DemoConfig } from './src/lib/demoConfig';
import { supabase } from './src/lib/supabase';
import { formatDayLabel, isReadOnlyUser, middayOf } from './src/logic/readOnly';
import { colors } from './src/theme';
import type { VisionClient } from './src/lib/supabase';

const Tab = createBottomTabNavigator();

const DEMO_REEL_CAPTION =
  "Cut overnight by the real nightly pipeline from this day's clips, with flagged " +
  'moments always included. Every clip is under Raw Footage.';

// Tests render without a native safe-area measurement; zero insets let the
// provider render its children on the first pass instead of waiting for one.
const ZERO_METRICS = {
  frame: { x: 0, y: 0, width: 0, height: 0 },
  insets: { top: 0, left: 0, right: 0, bottom: 0 },
};

export function AppRoot({
  client,
  demo = buildDemoConfig,
}: {
  client: VisionClient;
  demo?: DemoConfig | null;
}) {
  return (
    <SafeAreaProvider initialMetrics={initialWindowMetrics ?? ZERO_METRICS}>
      <AuthGate client={client} demo={demo} />
    </SafeAreaProvider>
  );
}

function AuthGate({ client, demo }: { client: VisionClient; demo: DemoConfig | null }) {
  const { state, signIn, signOut } = useAuth(client);

  if (state.kind === 'loading') {
    return (
      <View style={styles.splash}>
        <Text style={styles.splashTitle} accessibilityRole="header">
          Visio
        </Text>
        <StatusBar style="light" />
      </View>
    );
  }

  if (state.kind === 'signed-out') {
    return (
      <>
        <AuthContainer signIn={signIn} demo={demo} />
        <StatusBar style="light" />
      </>
    );
  }

  return <SignedInApp client={client} session={state.session} onSignOut={signOut} />;
}

function SignedInApp({
  client,
  session,
  onSignOut,
}: {
  client: VisionClient;
  session: Session;
  onSignOut: () => Promise<void>;
}) {
  const device = useDevice(client);
  const insets = useSafeAreaInsets();
  const readOnly = isReadOnlyUser(session.user);
  // The demo browses its newest reel's day rather than today, so a demo seeded
  // once keeps showing its footage. Owners always see today.
  const latest = useLatestReelDate(
    client,
    device.kind === 'ready' ? device.deviceId : '',
    readOnly && device.kind === 'ready',
  );
  const demoDay = readOnly && latest.kind === 'ready' ? latest.date : null;

  const banner = readOnly ? <DemoBanner onExit={() => void onSignOut()} /> : null;

  let body: React.ReactNode;
  if (
    device.kind === 'loading' ||
    (readOnly && device.kind === 'ready' && latest.kind === 'loading')
  ) {
    body = (
      <View style={styles.splash}>
        <Text style={styles.muted}>
          {readOnly ? 'Loading the demo...' : 'Loading your device...'}
        </Text>
      </View>
    );
  } else if (device.kind === 'error') {
    body = (
      <View style={styles.splash}>
        <Text style={styles.error} accessibilityLabel="Device lookup error">
          {device.message}
        </Text>
      </View>
    );
  } else if (device.kind === 'none') {
    body = (
      <View style={styles.splash}>
        <Text style={styles.muted}>No device linked to this account yet.</Text>
      </View>
    );
  } else {
    const deviceId = device.deviceId;
    const now = demoDay ? () => middayOf(demoDay) : undefined;
    body = (
      <NavigationContainer theme={DarkTheme}>
        <Tab.Navigator
          screenOptions={{
            headerShown: false,
            tabBarActiveTintColor: colors.accent,
            tabBarInactiveTintColor: colors.textMuted,
            tabBarStyle: [
              { backgroundColor: colors.surface, borderTopColor: colors.background },
              styles.webTabBar,
            ],
            // The label clips overflow to ellipsize, so it needs a line box tall
            // enough for descenders ("y", "g") and must not shrink under the icon.
            tabBarLabelStyle: styles.tabLabel,
          }}
        >
          <Tab.Screen name="Today's Reel" options={{ tabBarIcon: TodayReelIcon }}>
            {() => (
              <TodayReelContainer
                client={client}
                deviceId={deviceId}
                now={now}
                title={demoDay ? 'Sample reel' : undefined}
                subtitle={demoDay ? formatDayLabel(demoDay) : null}
                caption={demoDay ? DEMO_REEL_CAPTION : null}
              />
            )}
          </Tab.Screen>
          <Tab.Screen name="Raw Footage" options={{ tabBarIcon: RawFootageIcon }}>
            {() => (
              <RawFootageContainer
                client={client}
                deviceId={deviceId}
                now={now}
                readOnly={readOnly}
              />
            )}
          </Tab.Screen>
          <Tab.Screen name="Device" options={{ tabBarIcon: DeviceIcon }}>
            {() => (
              <DeviceStack
                client={client}
                deviceId={deviceId}
                deviceName={device.name}
                readOnly={readOnly}
              />
            )}
          </Tab.Screen>
          <Tab.Screen name="Archive" options={{ tabBarIcon: ArchiveIcon }}>
            {() => <ArchiveContainer client={client} deviceId={deviceId} now={now} />}
          </Tab.Screen>
        </Tab.Navigator>
      </NavigationContainer>
    );
  }

  return (
    <View style={[styles.root, { paddingTop: insets.top }]}>
      {banner}
      <View style={styles.root}>{body}</View>
      <StatusBar style="light" />
    </View>
  );
}

export default function App() {
  return (
    <WebFrame>
      <AppRoot client={supabase} />
    </WebFrame>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.background },
  splash: {
    flex: 1,
    backgroundColor: colors.background,
    alignItems: 'center',
    justifyContent: 'center',
  },
  splashTitle: { color: colors.text, fontSize: 40, fontWeight: '700' },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16 },
  tabLabel: { lineHeight: 14, flexShrink: 0 },
  // Web gets bottom-tabs' 48px default, which leaves 38px inside the item's
  // padding for a 28px icon slot and the 14px label above. Native keeps the
  // library's own safe-area-aware height.
  webTabBar: Platform.select({ web: { height: 52 }, default: {} }),
});
