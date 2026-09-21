import { Pin, Sparkles } from 'lucide-react';
import type React from 'react';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { ModelTier } from './profilesContext';

interface IntelligenceSelectorProps {
  /** Tiers the current profile lets the user choose between, in config order. */
  tiers: ModelTier[];
  /** The tier the profile runs at when the user chooses nothing. */
  defaultTierId: string | null;
  /** The user's choice, or null for the profile default. */
  selectedTierId: string | null;
  /** Whether that choice holds for the conversation rather than one message. */
  pinned: boolean;
  onChange: (tierId: string | null, pinned: boolean) => void;
  disabled?: boolean;
}

/**
 * Picks how much model capability to apply to the next request, beside the
 * profile picker that says which agent handles it.
 *
 * Renders nothing where the profile offers no choice — a pinned profile, or one
 * whose single allowed tier is the one it already runs at — rather than a dead
 * control the user can open and find nothing to decide in.
 */
const IntelligenceSelector: React.FC<IntelligenceSelectorProps> = ({
  tiers,
  defaultTierId,
  selectedTierId,
  pinned,
  onChange,
  disabled = false,
}) => {
  if (tiers.length < 2) {
    return null;
  }

  // Radix treats the empty string as "nothing selected", which is what a
  // profile that reports tiers but no default should show.
  const value = selectedTierId ?? defaultTierId ?? '';
  const activeTier = tiers.find((tier) => tier.id === value);
  const activeLabel = activeTier?.label ?? 'Intelligence';

  const handleTierChange = (tierId: string) => {
    // Choosing the default is choosing nothing: there is no selection to send,
    // and nothing to pin either.
    if (tierId === defaultTierId) {
      onChange(null, false);
      return;
    }
    onChange(tierId, pinned);
  };

  return (
    <div className="flex items-center gap-1" data-testid="intelligence-selector">
      <Select value={value} onValueChange={handleTierChange} disabled={disabled}>
        <SelectTrigger
          className="w-auto min-w-[110px] h-8 text-sm"
          aria-label="Intelligence"
          data-testid="intelligence-selector-trigger"
          title={
            selectedTierId === null
              ? `${activeLabel} is this profile's default intelligence`
              : pinned
                ? `${activeLabel} stays selected for this conversation`
                : `${activeLabel} applies to your next message`
          }
        >
          <div className="flex items-center gap-2">
            <Sparkles className="w-4 h-4" />
            <SelectValue>{activeLabel}</SelectValue>
          </div>
        </SelectTrigger>
        <SelectContent>
          {tiers.map((tier) => (
            <SelectItem key={tier.id} value={tier.id}>
              <div className="flex flex-col gap-1 py-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{tier.label}</span>
                  {tier.id === defaultTierId && (
                    <span className="text-xs text-muted-foreground">Default</span>
                  )}
                </div>
                {tier.description && (
                  <div className="text-xs text-muted-foreground max-w-[250px]">
                    {tier.description}
                  </div>
                )}
              </div>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {selectedTierId !== null && (
        <Button
          type="button"
          variant={pinned ? 'default' : 'ghost'}
          size="sm"
          className="h-8 px-2"
          disabled={disabled}
          aria-pressed={pinned}
          aria-label={
            pinned
              ? `Unpin ${activeLabel} from this conversation`
              : `Pin ${activeLabel} to this conversation`
          }
          title={
            pinned
              ? `${activeLabel} stays selected for this conversation. Click to use it for the next message only.`
              : `${activeLabel} applies to your next message only. Click to keep it for this conversation.`
          }
          data-testid="intelligence-pin"
          data-pinned={pinned ? 'true' : 'false'}
          onClick={() => onChange(selectedTierId, !pinned)}
        >
          <Pin className={`w-3.5 h-3.5 ${pinned ? 'fill-current' : ''}`} />
        </Button>
      )}
    </div>
  );
};

export default IntelligenceSelector;
