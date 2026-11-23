import { afterEach, describe, expect, it, vi } from 'vitest';
import { getStashService } from '@/services/stash/StashGraphQLService';

describe('StashGraphQLService', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('scans an exact file path instead of its parent directory', async () => {
    const stashService = getStashService();
    const scan = vi.spyOn(stashService, 'triggerScan').mockResolvedValue('scan-job');
    const filePath = '/stash/images/gallery-dl/TikTok/creator/photo.jpg';

    await expect(stashService.triggerScanForFile(filePath)).resolves.toBe('scan-job');

    expect(scan).toHaveBeenCalledWith([filePath]);
  });
});
