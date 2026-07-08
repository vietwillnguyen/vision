export interface Segment {
  id: string;
  recordedAt: string;
  durationSec: number;
  s3Key: string;
  manuallyFlagged: boolean;
  userFeedback: 'include' | 'exclude' | null;
}

export interface Reel {
  id: string;
  date: string;
  s3Key: string;
  durationSec: number;
  style: 'clean' | 'vintage';
}

export interface DeviceStatus {
  batteryPct: number;
  storageUsedGb: number;
  storageFreeGb: number;
  segmentsPending: number;
  segmentsUploadedToday: number;
  recordingActive: boolean;
}
