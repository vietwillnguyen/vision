import {
  buildSegmentSignedUrlRequest,
  InvalidSegmentKeyError,
} from '../../src/logic/segmentExport';

describe('buildSegmentSignedUrlRequest', () => {
  it('builds a request for the segments bucket with a default one-hour expiry', () => {
    expect(buildSegmentSignedUrlRequest('device-abc/seg-1.mp4')).toEqual({
      bucket: 'segments',
      path: 'device-abc/seg-1.mp4',
      expiresInSec: 3600,
    });
  });

  it('accepts a custom expiry', () => {
    expect(buildSegmentSignedUrlRequest('device-abc/seg-1.mp4', 300).expiresInSec).toBe(300);
  });

  it('rejects an empty key', () => {
    expect(() => buildSegmentSignedUrlRequest('')).toThrow(InvalidSegmentKeyError);
  });
});
