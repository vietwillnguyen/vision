export interface SignedUrlRequest {
  bucket: 'segments';
  path: string;
  expiresInSec: number;
}

export class InvalidSegmentKeyError extends Error {}

export function buildSegmentSignedUrlRequest(
  s3Key: string,
  expiresInSec: number = 3600,
): SignedUrlRequest {
  if (!s3Key) {
    throw new InvalidSegmentKeyError('segment key must not be empty');
  }
  return { bucket: 'segments', path: s3Key, expiresInSec };
}
