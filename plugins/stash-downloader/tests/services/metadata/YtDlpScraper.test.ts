import { afterEach, describe, expect, it, vi } from 'vitest';
import { YtDlpScraper } from '@/services/metadata/YtDlpScraper';
import { getStashService } from '@/services/stash/StashGraphQLService';
import { ContentType } from '@/types';

describe('YtDlpScraper', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('maps TikTok photo data to gallery metadata and prefers uploader over creator', async () => {
    const extractedMetadata = {
      success: true,
      title: 'TikTok photo post',
      uploader: 'creator_handle',
      creator: 'Creator Display Name',
      creators: ['Less Reliable Creator'],
      content_type: 'gallery',
      gallery_images: [
        { url: 'https://cdn.example/photo-1.jpg', filename: 'photo-1.jpg' },
        { url: 'https://cdn.example/photo-2.jpg', filename: 'photo-2.jpg' },
      ],
    };
    vi.spyOn(getStashService(), 'runPluginOperationWithError')
      .mockResolvedValue({ data: extractedMetadata });

    const metadata = await new YtDlpScraper().scrape('https://www.tiktok.com/@creator/photo/123456789');

    expect(metadata.contentType).toBe(ContentType.Gallery);
    expect(metadata.galleryImages?.map(image => image.url)).toEqual([
      'https://cdn.example/photo-1.jpg',
      'https://cdn.example/photo-2.jpg',
    ]);
    expect(metadata.performers).toEqual(['creator_handle']);
    expect(metadata.studio).toBeUndefined();
  });

  it('falls back to TikTok creator name when uploader is unavailable', async () => {
    const extractedMetadata = {
      success: true,
      title: 'TikTok video',
      creator: 'Creator Display Name',
    };
    vi.spyOn(getStashService(), 'runPluginOperationWithError')
      .mockResolvedValue({ data: extractedMetadata });

    const metadata = await new YtDlpScraper().scrape('https://www.tiktok.com/@creator/video/123456789');

    expect(metadata.performers).toEqual(['Creator Display Name']);
  });
});
