import { useVideoPlayer, VideoView } from 'expo-video';
import React from 'react';
import { ActivityIndicator, Button, StyleSheet, Text, View } from 'react-native';

import { usePauseOnBlur } from '../hooks/usePauseOnBlur';
import { colors, spacing } from '../theme';

interface ReelPlayerProps {
  videoUri: string | null;
  /** Set when the video cannot be loaded, so the player says so. */
  videoError?: string | null;
  title: string;
  subtitle?: string | null;
  /** A line of context under the player. */
  caption?: string | null;
  /** Omitted where the platform has no share sheet. */
  onShare?: () => void;
}

export function ReelPlayer({
  videoUri,
  videoError,
  title,
  subtitle,
  caption,
  onShare,
}: ReelPlayerProps) {
  // useVideoPlayer must be called unconditionally; it accepts a null source.
  const player = useVideoPlayer(videoUri);
  usePauseOnBlur(player);

  return (
    <View style={styles.container}>
      <Text style={styles.title} accessibilityRole="header">
        {title}
      </Text>
      {subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}
      {videoUri ? (
        <VideoView
          player={player}
          style={styles.video}
          nativeControls
          contentFit="contain"
          accessibilityLabel="Reel video player"
        />
      ) : (
        <View style={[styles.video, styles.placeholder]}>
          {videoError ? (
            <Text style={styles.error} accessibilityLabel="Reel video error">
              {videoError}
            </Text>
          ) : (
            <>
              <ActivityIndicator color={colors.textMuted} />
              <Text style={styles.muted}>Preparing playback...</Text>
            </>
          )}
        </View>
      )}
      {caption ? <Text style={styles.caption}>{caption}</Text> : null}
      {onShare ? (
        <View style={styles.buttonWrap}>
          <Button
            title="Share"
            color={colors.accent}
            accessibilityLabel="Share reel"
            onPress={onShare}
            disabled={!videoUri}
          />
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  title: { color: colors.text, fontSize: 20, fontWeight: '600' },
  subtitle: { color: colors.textMuted, fontSize: 14, marginTop: 2 },
  // The pendant records 1280x720 landscape and the reel keeps that shape.
  video: {
    width: '100%',
    aspectRatio: 16 / 9,
    borderRadius: 12,
    overflow: 'hidden',
    backgroundColor: colors.surface,
    marginTop: spacing.md,
  },
  placeholder: { alignItems: 'center', justifyContent: 'center', padding: spacing.md },
  muted: { color: colors.textMuted, marginTop: spacing.sm },
  error: { color: colors.danger, textAlign: 'center' },
  caption: { color: colors.textMuted, fontSize: 14, lineHeight: 20, marginTop: spacing.md },
  buttonWrap: { marginTop: spacing.md },
});
