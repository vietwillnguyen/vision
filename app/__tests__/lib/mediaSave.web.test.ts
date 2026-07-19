import * as mediaSaveWeb from '../../src/lib/mediaSave.web';

describe('mediaSave.web', () => {
  it('reports unavailable and rejects saveUrlToCameraRoll', async () => {
    expect(mediaSaveWeb.isAvailable).toBe(false);
    await expect(mediaSaveWeb.saveUrlToCameraRoll('https://example.com/seg.mp4', 'seg-1')).rejects.toThrow(
      'Saving to camera roll is not supported on web',
    );
  });
});
