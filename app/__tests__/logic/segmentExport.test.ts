import {
  buildSegmentSignedUrlRequest,
  InvalidExpiryError,
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

  it('accepts the expiry bounds of 1 and 86400 seconds', () => {
    expect(buildSegmentSignedUrlRequest('device-abc/seg-1.mp4', 1).expiresInSec).toBe(1);
    expect(buildSegmentSignedUrlRequest('device-abc/seg-1.mp4', 86400).expiresInSec).toBe(86400);
  });

  it.each([0, -1, 86401, NaN, Infinity])('rejects an invalid expiry of %p', (expiry) => {
    expect(() => buildSegmentSignedUrlRequest('device-abc/seg-1.mp4', expiry)).toThrow(
      InvalidExpiryError,
    );
  });
});
