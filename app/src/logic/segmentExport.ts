export interface SignedUrlRequest {
  bucket: 'segments' | 'reels';
  path: string;
  expiresInSec: number;
}

export class InvalidSegmentKeyError extends Error {}

export class InvalidExpiryError extends Error {}

const MAX_EXPIRY_SEC = 86400;

function buildSignedUrlRequest(
  bucket: SignedUrlRequest['bucket'],
  s3Key: string,
  expiresInSec: number,
): SignedUrlRequest {
  if (!s3Key) {
    throw new InvalidSegmentKeyError('segment key must not be empty');
  }
  if (!Number.isFinite(expiresInSec) || expiresInSec < 1 || expiresInSec > MAX_EXPIRY_SEC) {
    throw new InvalidExpiryError(`expiresInSec must be between 1 and ${MAX_EXPIRY_SEC}`);
  }
  return { bucket, path: s3Key, expiresInSec };
}

export function buildSegmentSignedUrlRequest(
  s3Key: string,
  expiresInSec: number = 3600,
): SignedUrlRequest {
  return buildSignedUrlRequest('segments', s3Key, expiresInSec);
}

export function buildReelSignedUrlRequest(
  s3Key: string,
  expiresInSec: number = 3600,
): SignedUrlRequest {
  return buildSignedUrlRequest('reels', s3Key, expiresInSec);
}
