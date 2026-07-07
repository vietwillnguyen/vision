import {
  InvalidRegenerateRequestError,
  validateRegenerateRequest,
} from '../../src/logic/regeneratePreferences';

describe('validateRegenerateRequest', () => {
  it('accepts a valid combination', () => {
    const result = validateRegenerateRequest({
      targetLengthSec: 90,
      style: 'vintage',
      mood: 'balanced',
    });
    expect(result).toEqual({ targetLengthSec: 90, style: 'vintage', mood: 'balanced' });
  });

  it('rejects an invalid target length', () => {
    expect(() =>
      validateRegenerateRequest({ targetLengthSec: 45, style: 'clean', mood: 'balanced' }),
    ).toThrow(InvalidRegenerateRequestError);
  });

  it('rejects an invalid style', () => {
    expect(() =>
      validateRegenerateRequest({ targetLengthSec: 60, style: 'sepia', mood: 'balanced' }),
    ).toThrow(InvalidRegenerateRequestError);
  });

  it('rejects an invalid mood', () => {
    expect(() =>
      validateRegenerateRequest({ targetLengthSec: 60, style: 'clean', mood: 'chaotic' }),
    ).toThrow(InvalidRegenerateRequestError);
  });
});
