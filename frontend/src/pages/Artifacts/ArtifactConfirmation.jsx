import React, { useState } from 'react';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

export const artifactKindLabels = {
  note: 'Note',
  script: 'Script',
  event: 'Event automation',
  schedule: 'Scheduled automation',
};

export const artifactStatus = (artifact) => {
  if (!['trusted_user', 'trusted_internal', 'machine_reviewed'].includes(artifact.trust_tier)) {
    return 'Needs review';
  }
  if (artifact.disposition === 'human_confirmed') {
    return 'User confirmed';
  }
  if (['trusted_user', 'trusted_internal'].includes(artifact.trust_tier)) {
    return 'Trusted';
  }
  if (artifact.trust_tier === 'machine_reviewed') {
    return 'Reviewed';
  }
  return 'Needs review';
};

export const loadArtifacts = async (kind) => {
  const url = kind ? `/api/artifacts/?kind=${encodeURIComponent(kind)}` : '/api/artifacts/';
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error('Failed to load artifacts');
  }
  return response.json();
};

export const ArtifactContent = ({ content }) => (
  <dl className="space-y-4">
    {Object.entries(content).map(([field, value]) => (
      <div key={field}>
        <dt className="mb-1 text-sm font-medium">{field.replaceAll('_', ' ')}</dt>
        <dd>
          <pre className="whitespace-pre-wrap break-words rounded border bg-muted/50 p-3 font-mono text-sm">
            {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
          </pre>
        </dd>
      </div>
    ))}
  </dl>
);

const ArtifactConfirmation = ({ artifact, onConfirmed }) => {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);

  const confirm = async () => {
    setPending(true);
    setError(null);
    try {
      const response = await fetch(`/api/artifacts/${artifact.kind}/${artifact.id}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content_hash: artifact.content_hash }),
      });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Failed to confirm artifact');
      }
      onConfirmed(await response.json());
      setOpen(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(value) => !pending && setOpen(value)}>
      <Button
        type="button"
        variant="outline"
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
        disabled={artifactStatus(artifact) === 'User confirmed'}
      >
        {artifactStatus(artifact) === 'User confirmed' ? 'User confirmed' : 'Review and confirm'}
      </Button>
      <DialogContent className="flex max-h-[90dvh] max-w-3xl flex-col">
        <DialogHeader>
          <DialogTitle>Confirm {artifact.name}</DialogTitle>
          <DialogDescription>
            Review the complete content below. Confirming marks this artifact as approved for reuse
            by the assistant. An automation’s referenced scripts and a note’s attachments keep their
            own trust status.
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 overflow-y-auto">
          <ArtifactContent content={artifact.content} />
        </div>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="secondary" onClick={() => setOpen(false)} disabled={pending}>
            Cancel
          </Button>
          <Button onClick={confirm} disabled={pending}>
            {pending ? 'Confirming...' : 'Confirm as user reviewed'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default ArtifactConfirmation;
