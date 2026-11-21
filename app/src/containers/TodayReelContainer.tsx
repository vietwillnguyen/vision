import React from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { ReelPlayer } from '../components/ReelPlayer';
import { useReel } from '../hooks/useReel';
import { useSignedUrlState } from '../hooks/useSignedUrl';
import { canShare, shareUrl } from '../lib/share';
import { utcDateString } from '../logic/dates';
import { buildReelSignedUrlRequest } from '../logic/segmentExport';
import { colors, spacing } from '../theme';
import type { VisionClient } from '../lib/supabase';

interface TodayReelContainerProps {
  client: VisionClient;
  deviceId: string;
  now?: () => Date;
  /** Heading over the player; the demo names its sample day instead. */
  title?: string;
  subtitle?: string | null;
  caption?: string | null;
}

export function TodayReelContainer({
  client,
  deviceId,
  now = () => new Date(),
  title = "Today's Reel",
  subtitle = null,
  caption = null,
}: TodayReelContainerProps) {
  const today = utcDateString(now());
  const reelState = useReel(client, deviceId, today);
  const video = useSignedUrlState(
    client,
    reelState.kind === 'ready' && reelState.reel.s3Key
      ? buildReelSignedUrlRequest(reelState.reel.s3Key)
      : null,
  );

  if (reelState.kind === 'loading') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Loading today&apos;s reel...</Text>
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
        <Text style={styles.muted}>Today&apos;s reel isn&apos;t ready yet.</Text>
      </View>
    );
  }

  const videoUri = video.kind === 'ready' ? video.url : null;
  const videoError = !reelState.reel.s3Key
    ? 'This reel has no video file yet.'
    : video.kind === 'error'
      ? `Couldn't load the reel video (${video.message}).`
      : null;
  return (
    <ReelPlayer
      videoUri={videoUri}
      videoError={videoError}
      title={title}
      subtitle={subtitle}
      caption={caption}
      onShare={canShare() ? () => videoUri && shareUrl(videoUri) : undefined}
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
