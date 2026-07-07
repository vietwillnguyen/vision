import React from 'react';
import { Pressable, ScrollView, Text, View } from 'react-native';

import type { TimelineSlot } from '../logic/timeline';

interface TimelineScrubberProps {
  slots: TimelineSlot[];
  onSlotLongPress: (slot: TimelineSlot) => void;
}

export function TimelineScrubber({ slots, onSlotLongPress }: TimelineScrubberProps) {
  return (
    <ScrollView horizontal testID="timeline-scrubber">
      {slots.map((slot) => (
        <Pressable
          key={slot.startMinute}
          testID={`slot-${slot.startMinute}`}
          onLongPress={() => onSlotLongPress(slot)}
        >
          <View>
            {slot.isFlagged ? <Text testID={`flag-${slot.startMinute}`}>🚩</Text> : null}
          </View>
        </Pressable>
      ))}
    </ScrollView>
  );
}
