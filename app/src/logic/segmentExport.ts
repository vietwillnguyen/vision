export interface SignedUrlRequest {
  bucket: 'segments';
  path: string;
  expiresInSec: number;
}

export class InvalidSegmentKeyError extends Error {}

export class InvalidExpiryError extends Error {}

const MAX_EXPIRY_SEC = 86400;

export function buildSegmentSignedUrlRequest(
  s3Key: string,
  expiresInSec: number = 3600,
): SignedUrlRequest {
  if (!s3Key) {
    throw new InvalidSegmentKeyError('segment key must not be empty');
  }
  if (!Number.isFinite(expiresInSec) || expiresInSec < 1 || expiresInSec > MAX_EXPIRY_SEC) {
    throw new InvalidExpiryError(`expiresInSec must be between 1 and ${MAX_EXPIRY_SEC}`);
  }
  return { bucket: 'segments', path: s3Key, expiresInSec };
}
