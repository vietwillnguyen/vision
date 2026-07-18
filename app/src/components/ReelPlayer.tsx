import { useVideoPlayer, VideoView } from 'expo-video';
import React from 'react';
import { Button, StyleSheet, Text, View } from 'react-native';

import { colors, spacing } from '../theme';

interface ReelPlayerProps {
  videoUri: string | null;
  title: string;
  onShare: () => void;
}

export function ReelPlayer({ videoUri, title, onShare }: ReelPlayerProps) {
  // useVideoPlayer must be called unconditionally; it accepts a null source.
  const player = useVideoPlayer(videoUri);

  return (
    <View style={styles.container}>
      <Text style={styles.title} accessibilityRole="header">
        {title}
      </Text>
      {videoUri ? (
        <VideoView
          player={player}
          style={styles.video}
          nativeControls
          accessibilityLabel="Reel video player"
        />
      ) : (
        <View style={[styles.video, styles.placeholder]}>
          <Text style={styles.muted}>Preparing playback...</Text>
        </View>
      )}
      <View style={styles.buttonWrap}>
        <Button title="Share" color={colors.accent} accessibilityLabel="Share reel" onPress={onShare} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  title: { color: colors.text, fontSize: 20, fontWeight: '600', marginBottom: spacing.md },
  video: { width: '100%', aspectRatio: 9 / 16, borderRadius: 12, backgroundColor: colors.surface },
  placeholder: { alignItems: 'center', justifyContent: 'center' },
  muted: { color: colors.textMuted },
  buttonWrap: { marginTop: spacing.md },
});
