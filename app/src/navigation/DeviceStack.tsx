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
  paired: boolean;
  onDeviceChanged: () => void;
}

export function DeviceStack({ client, deviceId, paired, onDeviceChanged }: DeviceStackProps) {
  return (
    <Stack.Navigator screenOptions={{ headerShown: false }}>
      <Stack.Screen name="DeviceHome">
        {(props) => (
          <DeviceContainer
            {...props}
            client={client}
            deviceId={deviceId}
            paired={paired}
            onDeviceChanged={onDeviceChanged}
          />
        )}
      </Stack.Screen>
      <Stack.Screen name="Reonboard">
        {({ navigation }) => (
          <ReonboardContainer
            client={client}
            deviceId={deviceId}
            onDone={() => {
              navigation.goBack();
              onDeviceChanged();
            }}
          />
        )}
      </Stack.Screen>
    </Stack.Navigator>
  );
}
