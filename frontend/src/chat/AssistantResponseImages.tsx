import { useMessage } from '@assistant-ui/react';
import { ImageOffIcon } from 'lucide-react';
import React, { useMemo, useState } from 'react';
import { collectResponseImages, type ResponseImage } from './responseImages';

const ImageTile: React.FC<{ image: ResponseImage }> = ({ image }) => {
  const [failed, setFailed] = useState(false);

  // A broken image must not vanish silently: fall back to a labelled link so the
  // attachment is still reachable.
  if (failed) {
    return (
      <a
        href={image.url}
        target="_blank"
        rel="noopener noreferrer"
        className="flex items-center gap-2 rounded-lg border border-border/60 px-3 py-2 text-xs text-muted-foreground hover:bg-accent/50"
        data-testid="response-image-unavailable"
      >
        <ImageOffIcon size={14} />
        <span className="truncate">{image.name} (preview unavailable)</span>
      </a>
    );
  }

  return (
    <a
      href={image.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block"
      title={image.name}
    >
      <img
        src={image.url}
        alt={image.name}
        className="max-h-80 max-w-full rounded-lg border border-border/60 object-contain transition-opacity hover:opacity-90"
        onError={() => setFailed(true)}
        data-testid="response-image"
      />
    </a>
  );
};

/**
 * Images the assistant attached to its reply, rendered inline in the response.
 *
 * The attachments themselves ride along as `attach_to_response` tool-call parts,
 * which live inside a tool group that collapses once the turn completes — so
 * without this the user only sees a paperclip they have to expand. Non-image
 * attachments stay in the tool group, where the name and download link are.
 */
export const AssistantResponseImages: React.FC = () => {
  // Serialize inside the selector so a fresh array on every store read doesn't
  // retrigger a render (same reason ToolGroup does this).
  const serialized = useMessage<string>({
    optional: true,
    selector: (message) => JSON.stringify(collectResponseImages(message.content)),
  });
  const images = useMemo<ResponseImage[]>(
    () => (serialized ? (JSON.parse(serialized) as ResponseImage[]) : []),
    [serialized]
  );

  if (images.length === 0) {
    return null;
  }

  return (
    <div className="mt-2 flex flex-wrap gap-3" data-testid="assistant-response-images">
      {images.map((image) => (
        <ImageTile key={image.key} image={image} />
      ))}
    </div>
  );
};
