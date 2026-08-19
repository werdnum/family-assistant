import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ImageLightbox, type LightboxImage } from '../image-lightbox';

const IMAGES: LightboxImage[] = [
  { key: 'a', url: '/api/attachments/a', name: 'First photo' },
  { key: 'b', url: '/api/attachments/b', name: 'Second photo' },
  { key: 'c', url: '/api/attachments/c', name: 'Third photo' },
];

/** Drives the lightbox the way its callers do, so navigation is observable. */
const Harness: React.FC<{ images?: LightboxImage[]; initialIndex?: number | null }> = ({
  images = IMAGES,
  initialIndex = 0,
}) => {
  const [index, setIndex] = useState<number | null>(initialIndex);
  return (
    <ImageLightbox
      images={images}
      index={index}
      onIndexChange={setIndex}
      onClose={() => setIndex(null)}
    />
  );
};

describe('ImageLightbox', () => {
  it('renders nothing while closed', () => {
    render(<Harness initialIndex={null} />);

    expect(screen.queryByTestId('image-lightbox')).not.toBeInTheDocument();
  });

  it('shows the selected image with its caption and a download link', () => {
    render(<Harness initialIndex={1} />);

    const image = screen.getByTestId('image-lightbox-image');
    expect(image).toHaveAttribute('src', '/api/attachments/b');
    expect(image).toHaveAttribute('alt', 'Second photo');
    expect(screen.getByTestId('image-lightbox-caption')).toHaveTextContent('Second photo');
    expect(screen.getByTestId('image-lightbox-counter')).toHaveTextContent('2 / 3');
    expect(screen.getByTestId('image-lightbox-download')).toHaveAttribute(
      'href',
      '/api/attachments/b'
    );
  });

  it('steps between images with the navigation buttons, wrapping around', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByTestId('image-lightbox-next'));
    expect(screen.getByTestId('image-lightbox-image')).toHaveAttribute('src', '/api/attachments/b');

    await user.click(screen.getByTestId('image-lightbox-previous'));
    await user.click(screen.getByTestId('image-lightbox-previous'));
    expect(screen.getByTestId('image-lightbox-image')).toHaveAttribute('src', '/api/attachments/c');
  });

  it('steps between images with the arrow keys', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.keyboard('{ArrowRight}');
    expect(screen.getByTestId('image-lightbox-image')).toHaveAttribute('src', '/api/attachments/b');

    await user.keyboard('{ArrowLeft}');
    expect(screen.getByTestId('image-lightbox-image')).toHaveAttribute('src', '/api/attachments/a');
  });

  it('hides the navigation affordances for a single image', () => {
    render(<Harness images={[IMAGES[0]]} />);

    expect(screen.queryByTestId('image-lightbox-next')).not.toBeInTheDocument();
    expect(screen.queryByTestId('image-lightbox-previous')).not.toBeInTheDocument();
    expect(screen.queryByTestId('image-lightbox-counter')).not.toBeInTheDocument();
  });

  it('toggles zoom, and returns to fit when another image is shown', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByTestId('image-lightbox-zoom'));
    expect(screen.getByTestId('image-lightbox-image')).toHaveAttribute('data-zoomed', 'true');

    // Auto margins rather than centred justification, so the top-left of an
    // image larger than the viewport stays inside the scrollable area.
    const frame = screen.getByTestId('image-lightbox-zoom').parentElement;
    expect(frame).toHaveClass('overflow-auto', 'items-start', 'justify-start');
    expect(screen.getByTestId('image-lightbox-zoom')).toHaveClass('m-auto');

    await user.click(screen.getByTestId('image-lightbox-next'));
    expect(screen.getByTestId('image-lightbox-image')).toHaveAttribute('data-zoomed', 'false');
  });

  it('closes when the backdrop around the image is clicked', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<ImageLightbox images={IMAGES} index={0} onIndexChange={vi.fn()} onClose={onClose} />);

    // The content fills the viewport, so the blank area around the image is
    // part of it rather than of the Radix overlay.
    await user.click(screen.getByTestId('image-lightbox'));
    expect(onClose).toHaveBeenCalled();
  });

  it('does not close when the image or a control is clicked', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<ImageLightbox images={IMAGES} index={0} onIndexChange={vi.fn()} onClose={onClose} />);

    await user.click(screen.getByTestId('image-lightbox-image'));
    await user.click(screen.getByTestId('image-lightbox-next'));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on the close button and on Escape', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<ImageLightbox images={IMAGES} index={0} onIndexChange={vi.fn()} onClose={onClose} />);

    await user.click(screen.getByTestId('image-lightbox-close'));
    expect(onClose).toHaveBeenCalled();

    onClose.mockClear();
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalled();
  });
});
