import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';
import { Share, StyleSheet, Text, View } from 'react-native';

import { ReelPlayer } from '../components/ReelPlayer';
import { useReel } from '../hooks/useReel';
import { useSignedUrl } from '../hooks/useSignedUrl';
import { utcDateString } from '../logic/dates';
import { buildReelSignedUrlRequest } from '../logic/segmentExport';
import { colors, spacing } from '../theme';

interface TodayReelContainerProps {
  client: SupabaseClient;
  deviceId: string;
  now?: () => Date;
}

export function TodayReelContainer({ client, deviceId, now = () => new Date() }: TodayReelContainerProps) {
  const today = utcDateString(now());
  const reelState = useReel(client, deviceId, today);
  const signedUrl = useSignedUrl(
    client,
    reelState.kind === 'ready' ? buildReelSignedUrlRequest(reelState.reel.s3Key) : null,
  );

  if (reelState.kind === 'loading') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Loading today's reel...</Text>
      </View>
    );
  }
  if (reelState.kind === 'error') {
    return (
      <View style={styles.message}>
        <Text style={styles.error} accessibilityLabel="Reel error">
          {reelState.message}
        </Text>
      </View>
    );
  }
  if (reelState.kind === 'none') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Today's reel isn't ready yet.</Text>
      </View>
    );
  }

  return (
    <ReelPlayer
      videoUri={signedUrl}
      title="Today's Reel"
      onShare={() => {
        if (signedUrl) {
          Share.share({ url: signedUrl, message: signedUrl });
        }
      }}
    />
  );
}

const styles = StyleSheet.create({
  message: {
    flex: 1,
    backgroundColor: colors.background,
    alignItems: 'center',
    justifyContent: 'center',
    padding: spacing.lg,
  },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16 },
});
