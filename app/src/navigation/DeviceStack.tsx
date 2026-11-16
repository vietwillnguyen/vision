import { createNativeStackNavigator } from '@react-navigation/native-stack';
import React from 'react';

import { DeviceContainer } from '../containers/DeviceContainer';
import { ReonboardContainer } from '../containers/ReonboardContainer';
import type { VisionClient } from '../lib/supabase';

export type DeviceStackParamList = {
  DeviceHome: undefined;
  Reonboard: undefined;
};

const Stack = createNativeStackNavigator<DeviceStackParamList>();

interface DeviceStackProps {
  client: VisionClient;
  deviceId: string;
}

export function DeviceStack({ client, deviceId }: DeviceStackProps) {
  return (
    <Stack.Navigator screenOptions={{ headerShown: false }}>
      <Stack.Screen name="DeviceHome">
        {(props) => <DeviceContainer {...props} client={client} deviceId={deviceId} />}
      </Stack.Screen>
      <Stack.Screen name="Reonboard">
        {(props) => <ReonboardContainer {...props} client={client} />}
      </Stack.Screen>
    </Stack.Navigator>
  );
}
