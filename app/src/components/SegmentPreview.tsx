import React from 'react';
import { Button, Text, View } from 'react-native';

import type { Segment } from '../types';

interface SegmentPreviewProps {
  segment: Segment | null;
  onSave: () => void;
  onShare: () => void;
  onClose: () => void;
}

export function SegmentPreview({ segment, onSave, onShare, onClose }: SegmentPreviewProps) {
  if (!segment) {
    return null;
  }

  return (
    <View testID="segment-preview">
      <Text>{segment.recordedAt}</Text>
      <Button testID="save-button" title="Save to camera roll" onPress={onSave} />
      <Button testID="share-button" title="Share" onPress={onShare} />
      <Button testID="close-button" title="Close" onPress={onClose} />
    </View>
  );
}
