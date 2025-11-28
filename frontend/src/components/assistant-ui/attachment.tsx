'use client';

import {
  AttachmentPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  useAttachment,
  useComposerRuntime,
} from '@assistant-ui/react';
import { DialogContent as DialogPrimitiveContent } from '@radix-ui/react-dialog';
import { CircleXIcon, ClockIcon, FileIcon, PaperclipIcon } from 'lucide-react';
import { type FC, PropsWithChildren, useEffect, useState } from 'react';
import { useShallow } from 'zustand/shallow';
import { TooltipIconButton } from '@/components/assistant-ui/tooltip-icon-button';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import {
  Dialog,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
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

type AttachmentPreviewProps = {
  src: string;
};

const AttachmentPreview: FC<AttachmentPreviewProps> = ({ src }) => {
  const [isLoaded, setIsLoaded] = useState(false);

  return (
    <img
      src={src}
      style={{
        width: 'auto',
        height: 'auto',
        maxWidth: '75dvh',
        maxHeight: '75dvh',
        display: isLoaded ? 'block' : 'none',
        overflow: 'clip',
      }}
      onLoad={() => setIsLoaded(true)}
      alt="Preview"
    />
  );
};

const AttachmentPreviewDialog: FC<PropsWithChildren> = ({ children }) => {
  const src = useAttachmentSrc();

  if (!src) {
    return children;
  }

  return (
    <Dialog>
      <DialogTrigger className="hover:bg-accent/50 cursor-pointer transition-colors" asChild>
        {children}
      </DialogTrigger>
      <AttachmentDialogContent>
        <DialogTitle className="sr-only">Image Attachment Preview</DialogTitle>
        <AttachmentPreview src={src} />
      </AttachmentDialogContent>
    </Dialog>
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
      default: {
        const _exhaustiveCheck: never = type;
        throw new Error(`Unknown attachment type: ${_exhaustiveCheck}`);
      }
    }
  });

  // Check if attachment has an error status
  // @ts-expect-error - status.type may include 'error' at runtime
  const hasError = status?.type === 'error';
  const errorMessage = (status as { error?: string })?.error;

  // Check if attachment is currently uploading
  const isLoading = status?.type === 'running' && !hasError;

  return (
    <Tooltip>
      <AttachmentPrimitive.Root className="relative mt-3" data-testid="attachment-preview">
        {isLoading ? (
          // Show loading state during upload - still clickable for preview
          <AttachmentPreviewDialog>
            <TooltipTrigger asChild>
              <div className="flex flex-col gap-1">
                <div
                  className="flex h-12 w-40 items-center justify-center gap-2 rounded-lg border-2 border-blue-500 bg-blue-50 p-1 animate-pulse cursor-pointer hover:bg-blue-100 transition-colors"
                  data-testid="attachment-preview attachment-loading"
                >
                  <AttachmentThumb />
                  <div className="flex-grow basis-0">
                    <p className="text-muted-foreground line-clamp-1 text-ellipsis break-all text-xs font-bold">
                      <AttachmentPrimitive.Name />
                    </p>
                    <div className="flex items-center gap-1">
                      <ClockIcon size={12} className="animate-spin text-blue-600" />
                      <p className="text-blue-600 text-xs font-medium">Uploading...</p>
                    </div>
                  </div>
                </div>
              </div>
            </TooltipTrigger>
          </AttachmentPreviewDialog>
        ) : hasError ? (
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
          <AttachmentPreviewDialog>
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
          </AttachmentPreviewDialog>
        )}
        {canRemove && <AttachmentRemove />}
      </AttachmentPrimitive.Root>
      <TooltipContent side="top">
        {isLoading ? 'Uploading file...' : hasError ? errorMessage : <AttachmentPrimitive.Name />}
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
        accept="image/png,image/jpeg,image/gif,image/webp,text/plain,text/markdown,application/pdf"
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

const AttachmentDialogContent: FC<PropsWithChildren> = ({ children }) => (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitiveContent className="data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[state=closed]:slide-out-to-left-1/2 data-[state=closed]:slide-out-to-top-[48%] data-[state=open]:slide-in-from-left-1/2 data-[state=open]:slide-in-from-top-[48%] fixed left-[50%] top-[50%] z-50 grid translate-x-[-50%] translate-y-[-50%] shadow-lg duration-200">
      {children}
    </DialogPrimitiveContent>
  </DialogPortal>
);
