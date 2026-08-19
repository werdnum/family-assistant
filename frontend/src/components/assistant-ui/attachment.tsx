'use client';

import {
  AttachmentPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  useAttachment,
  useComposerRuntime,
} from '@assistant-ui/react';
import { Slot } from '@radix-ui/react-slot';
import { CircleXIcon, FileIcon, PaperclipIcon } from 'lucide-react';
import { type FC, PropsWithChildren, useEffect, useMemo, useState } from 'react';
import { useShallow } from 'zustand/shallow';
import { TooltipIconButton } from '@/components/assistant-ui/tooltip-icon-button';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { ImageLightbox } from '@/components/ui/image-lightbox';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

const useFileSrc = (file: File | undefined) => {
  const [src, setSrc] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!file) {
      setSrc(undefined);
      return;
    }

    const objectUrl = URL.createObjectURL(file);
    setSrc(objectUrl);

    return () => {
      URL.revokeObjectURL(objectUrl);
    };
  }, [file]);

  return src;
};

const useAttachmentSrc = () => {
  const { file, src } = useAttachment(
    useShallow((a): { file?: File; src?: string } => {
      if (a.type !== 'image') {
        return {};
      }
      if (a.file) {
        return { file: a.file };
      }
      // Handle both string content (base64) and array content formats
      if (typeof a.content === 'string') {
        // If content is a string (base64 data URL), use it directly
        return { src: a.content };
      } else if (Array.isArray(a.content)) {
        // If content is an array, look for image content
        const src = a.content.filter((c) => c.type === 'image')[0]?.image;
        if (!src) {
          return {};
        }
        return { src };
      }
      return {};
    })
  );

  return useFileSrc(file) ?? src;
};

/**
 * Wraps an image attachment so clicking it opens the full-screen lightbox.
 * Non-image attachments have nothing to show, so they render as-is.
 */
const AttachmentPreviewTrigger: FC<PropsWithChildren> = ({ children }) => {
  const src = useAttachmentSrc();
  const name = useAttachment((a) => a.name);
  const [isOpen, setIsOpen] = useState(false);
  const images = useMemo(
    () => (src ? [{ key: src, url: src, name: name || 'Image attachment' }] : []),
    [src, name]
  );

  if (!src) {
    return children;
  }

  return (
    <>
      <Slot
        role="button"
        tabIndex={0}
        className="hover:bg-accent/50 cursor-zoom-in transition-colors focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => setIsOpen(true)}
        onKeyDown={(event: React.KeyboardEvent) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            setIsOpen(true);
          }
        }}
        aria-label={`View ${name || 'image attachment'}`}
        data-testid="attachment-preview-trigger"
      >
        {children}
      </Slot>
      <ImageLightbox
        images={images}
        index={isOpen ? 0 : null}
        onIndexChange={() => undefined}
        onClose={() => setIsOpen(false)}
      />
    </>
  );
};

const AttachmentThumb: FC = () => {
  const isImage = useAttachment((a) => a.type === 'image');
  const src = useAttachmentSrc();
  return (
    <Avatar className="bg-muted flex size-10 items-center justify-center rounded border text-sm">
      <AvatarFallback delayMs={isImage ? 200 : 0}>
        <FileIcon />
      </AvatarFallback>
      <AvatarImage src={src} />
    </Avatar>
  );
};

const AttachmentUI: FC = () => {
  const canRemove = useAttachment((a) => a.source !== 'message');
  const status = useAttachment((a) => a.status);
  const typeLabel = useAttachment((a) => {
    const type = a.type;
    switch (type) {
      case 'image':
        return 'Image';
      case 'document':
        return 'Document';
      case 'file':
        return 'File';
    }
  });

  // Validation and upload failures land here: the runtime's terminal state for
  // an attachment that will not complete.
  const hasError = status?.type === 'incomplete' && status.reason === 'error';
  const errorMessage = hasError ? status.message : undefined;

  return (
    <Tooltip>
      <AttachmentPrimitive.Root className="relative mt-3" data-testid="attachment-preview">
        {hasError ? (
          // Show error state prominently
          <div className="flex flex-col gap-1">
            <div
              className="flex h-12 w-40 items-center justify-center gap-2 rounded-lg border border-red-500 bg-red-50 p-1"
              data-testid="attachment-error"
            >
              <AttachmentThumb />
              <div className="flex-grow basis-0">
                <p className="text-muted-foreground line-clamp-1 text-ellipsis break-all text-xs font-bold">
                  <AttachmentPrimitive.Name />
                </p>
                <p className="text-red-600 text-xs font-medium">Error</p>
              </div>
            </div>
            <p className="text-red-600 text-xs px-1" data-testid="attachment-error-message">
              {errorMessage}
            </p>
          </div>
        ) : (
          <AttachmentPreviewTrigger>
            <TooltipTrigger asChild>
              <div className="flex h-12 w-40 items-center justify-center gap-2 rounded-lg border p-1">
                <AttachmentThumb />
                <div className="flex-grow basis-0">
                  <p className="text-muted-foreground line-clamp-1 text-ellipsis break-all text-xs font-bold">
                    <AttachmentPrimitive.Name />
                  </p>
                  <p className="text-muted-foreground text-xs">{typeLabel}</p>
                </div>
              </div>
            </TooltipTrigger>
          </AttachmentPreviewTrigger>
        )}
        {canRemove && <AttachmentRemove />}
      </AttachmentPrimitive.Root>
      <TooltipContent side="top">
        {hasError ? errorMessage : <AttachmentPrimitive.Name />}
      </TooltipContent>
    </Tooltip>
  );
};

const AttachmentRemove: FC = () => {
  // AttachmentPrimitive.Remove handles the removal automatically
  // We just need to wrap it with our styled button
  return (
    <AttachmentPrimitive.Remove asChild>
      <TooltipIconButton
        tooltip="Remove file"
        className="text-muted-foreground [&>svg]:bg-background absolute -right-3 -top-3 size-6 [&>svg]:size-4 [&>svg]:rounded-full"
        side="top"
        aria-label="Remove attachment"
        data-testid="remove-attachment-button"
      >
        <CircleXIcon />
      </TooltipIconButton>
    </AttachmentPrimitive.Remove>
  );
};

export const UserMessageAttachments: FC = () => {
  return (
    <div className="flex w-full flex-row gap-3 col-span-full col-start-1 row-start-1 justify-end">
      <MessagePrimitive.Attachments components={{ Attachment: AttachmentUI }} />
    </div>
  );
};

export const AssistantMessageAttachments: FC = () => {
  return (
    <div className="flex w-full flex-row gap-3 col-span-full col-start-1 row-start-1 justify-start">
      <MessagePrimitive.Attachments components={{ Attachment: AttachmentUI }} />
    </div>
  );
};

export const ComposerAttachments: FC = () => {
  return (
    <div className="flex w-full flex-row gap-3 overflow-x-auto">
      <ComposerPrimitive.Attachments components={{ Attachment: AttachmentUI }} />
    </div>
  );
};

export const ComposerAddAttachment: FC = () => {
  const composerRuntime = useComposerRuntime();

  const handleFileSelect = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (files && files.length > 0) {
      // Add each file as an attachment
      Array.from(files).forEach((file) => {
        composerRuntime.addAttachment(file);
      });
      // Clear the input so the same file can be selected again
      event.target.value = '';
    }
  };

  return (
    <>
      <input
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp,text/plain,text/markdown,application/pdf,audio/mpeg,audio/wav,audio/ogg,audio/webm,video/mp4,video/webm,video/ogg"
        multiple
        className="hidden"
        id="composer-file-input"
        data-testid="file-input"
        onChange={handleFileSelect}
      />
      <TooltipIconButton
        className="my-2.5 size-8 p-2 transition-opacity ease-in"
        tooltip="Add Attachment"
        variant="ghost"
        type="button"
        data-testid="add-attachment-button"
        onClick={() => {
          const fileInput = document.getElementById('composer-file-input') as HTMLInputElement;
          if (fileInput) {
            fileInput.click();
          }
        }}
      >
        <PaperclipIcon />
      </TooltipIconButton>
    </>
  );
};
