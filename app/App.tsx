import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { NavigationContainer, DarkTheme } from '@react-navigation/native';
import { StatusBar } from 'expo-status-bar';
import React from 'react';
import { Platform, StyleSheet, Text, View } from 'react-native';

import { ArchiveContainer } from './src/containers/ArchiveContainer';
import { AuthContainer } from './src/containers/AuthContainer';
import { ArchiveIcon, DeviceIcon, RawFootageIcon, TodayReelIcon } from './src/components/TabIcons';
import { DeviceStack } from './src/navigation/DeviceStack';
import { RawFootageContainer } from './src/containers/RawFootageContainer';
import { TodayReelContainer } from './src/containers/TodayReelContainer';
import { useAuth } from './src/hooks/useAuth';
import { useDevice } from './src/hooks/useDevice';
import { supabase } from './src/lib/supabase';
import { colors } from './src/theme';
import type { VisionClient } from './src/lib/supabase';

const Tab = createBottomTabNavigator();

export function AppRoot({ client }: { client: VisionClient }) {
  const { state, signIn } = useAuth(client);

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
        <AuthContainer signIn={signIn} />
        <StatusBar style="light" />
      </>
    );
  }

  return <SignedInApp client={client} />;
}

function SignedInApp({ client }: { client: VisionClient }) {
  const device = useDevice(client);

  if (device.kind === 'loading') {
    return (
      <View style={styles.splash}>
        <Text style={styles.muted}>Loading your device...</Text>
      </View>
    );
  }
  if (device.kind === 'error') {
    return (
      <View style={styles.splash}>
        <Text style={styles.error} accessibilityLabel="Device lookup error">
          {device.message}
        </Text>
      </View>
    );
  }
  if (device.kind === 'none') {
    return (
      <View style={styles.splash}>
        <Text style={styles.muted}>No device linked to this account yet.</Text>
      </View>
    );
  }

  const deviceId = device.deviceId;
  return (
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
          {() => <TodayReelContainer client={client} deviceId={deviceId} />}
        </Tab.Screen>
        <Tab.Screen name="Raw Footage" options={{ tabBarIcon: RawFootageIcon }}>
          {() => <RawFootageContainer client={client} deviceId={deviceId} />}
        </Tab.Screen>
        <Tab.Screen name="Device" options={{ tabBarIcon: DeviceIcon }}>
          {() => <DeviceStack client={client} deviceId={deviceId} />}
        </Tab.Screen>
        <Tab.Screen name="Archive" options={{ tabBarIcon: ArchiveIcon }}>
          {() => <ArchiveContainer client={client} deviceId={deviceId} />}
        </Tab.Screen>
      </Tab.Navigator>
      <StatusBar style="light" />
    </NavigationContainer>
  );
}

export default function App() {
  return <AppRoot client={supabase} />;
}

const styles = StyleSheet.create({
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
