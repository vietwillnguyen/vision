import { useVideoPlayer, VideoView } from 'expo-video';
import React from 'react';
import { Button, StyleSheet, Text, View } from 'react-native';

import type { Segment } from '../types';
import { colors, spacing } from '../theme';

interface SegmentPreviewProps {
  segment: Segment | null;
  videoUri: string | null;
  onSave: () => void;
  onShare: () => void;
  onClose: () => void;
}

export function SegmentPreview({ segment, videoUri, onSave, onShare, onClose }: SegmentPreviewProps) {
  // Must run unconditionally before any early return (rules of hooks).
  const player = useVideoPlayer(videoUri);

  if (!segment) {
    return null;
  }

  return (
    <View testID="segment-preview" style={styles.container}>
      <Text style={styles.timestamp}>{segment.recordedAt}</Text>
      {videoUri ? (
        <VideoView
          player={player}
          style={styles.video}
          nativeControls
          accessibilityLabel="Segment video player"
        />
      ) : (
        <View style={[styles.video, styles.placeholder]}>
          <Text style={styles.muted}>Preparing playback...</Text>
        </View>
      )}
      <Button testID="save-button" title="Save to camera roll" color={colors.accent} accessibilityLabel="Save segment to camera roll" onPress={onSave} />
      <Button testID="share-button" title="Share" color={colors.accent} accessibilityLabel="Share segment" onPress={onShare} />
      <Button testID="close-button" title="Close" color={colors.textMuted} accessibilityLabel="Close preview" onPress={onClose} />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { backgroundColor: colors.surface, borderRadius: 12, padding: spacing.md },
  timestamp: { color: colors.textMuted, marginBottom: spacing.sm },
  video: { width: '100%', aspectRatio: 16 / 9, borderRadius: 8, backgroundColor: colors.background, marginBottom: spacing.sm },
  placeholder: { alignItems: 'center', justifyContent: 'center' },
  muted: { color: colors.textMuted },
});
