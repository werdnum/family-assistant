import React, { useEffect, useState } from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { fetchHome } from '../../../chat/threadsApi';

/**
 * Whether threads are on. A failed lookup reads as off: the option then keeps
 * its older wording, which still says what a run does apart from where.
 */
function useThreadsEnabled(): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetchHome(controller.signal)
      .then((home) => setEnabled(home.enabled))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          console.error('Error checking whether threads are on:', error);
        }
      });
    return () => controller.abort();
  }, []);
  return enabled;
}

interface NewRunOptionProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}

/**
 * The "each run starts afresh" choice for an automation that wakes the
 * assistant. With threads on, each run starts a thread off Home.
 */
export const NewRunOption: React.FC<NewRunOptionProps> = ({ checked, onCheckedChange }) => {
  const threads = useThreadsEnabled();
  return (
    <div className="space-y-2">
      <div className="flex items-center space-x-2">
        <Checkbox
          id="new_conversation"
          checked={checked}
          onCheckedChange={(value) => onCheckedChange(value === true)}
        />
        <Label htmlFor="new_conversation" className="font-normal">
          {threads ? 'Start a new thread each time' : 'Start a new conversation each time'}
        </Label>
      </div>
      <p className="text-sm text-muted-foreground pl-6">
        {threads
          ? 'Each run opens its own thread off Home instead of continuing an existing conversation'
          : 'Each run opens its own conversation instead of continuing an existing one'}
        , so the prompt should say everything the assistant needs.
      </p>
    </div>
  );
};
