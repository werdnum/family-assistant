import { ChevronDown, Sparkles } from 'lucide-react';
import type React from 'react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { type ModelTier, offersIntelligenceChoice } from './profilesContext';

interface IntelligenceSelectorProps {
  /** Tiers the current profile lets the user choose between, in config order. */
  tiers: ModelTier[];
  /**
   * Specific models the profile may also run on. Offered in a submenu, out of
   * the way of the tier choice most conversations never leave.
   */
  presets?: ModelTier[];
  /** The tier the profile runs at when the user chooses nothing. */
  defaultTierId: string | null;
  /** The tier the conversation is on, or null for the profile default. */
  selectedTierId: string | null;
  onChange: (tierId: string | null) => void;
  disabled?: boolean;
}

const EMPTY_PRESETS: ModelTier[] = [];

/**
 * Picks how much model capability the conversation runs at, beside the profile
 * picker that says which agent handles it. A choice holds for the rest of the
 * conversation: switching tiers turn by turn would make each switched turn
 * start without the provider's cached copy of the conversation.
 *
 * A specific model is chosen the same way and holds the same way; it sits in a
 * "Specific model" submenu so the everyday choice stays a short list.
 *
 * Renders nothing where the profile offers no choice — a pinned profile, or one
 * whose single allowed tier is the one it already runs at — rather than a dead
 * control the user can open and find nothing to decide in.
 */
const IntelligenceSelector: React.FC<IntelligenceSelectorProps> = ({
  tiers,
  presets = EMPTY_PRESETS,
  defaultTierId,
  selectedTierId,
  onChange,
  disabled = false,
}) => {
  if (!offersIntelligenceChoice(tiers, presets)) {
    return null;
  }

  const value = selectedTierId ?? defaultTierId ?? '';
  const activePreset = presets.find((preset) => preset.id === value);
  const activeLabel =
    tiers.find((tier) => tier.id === value)?.label ?? activePreset?.label ?? 'Intelligence';

  const handleChange = (tierId: string) => {
    // Choosing the default is choosing nothing.
    onChange(tierId === defaultTierId ? null : tierId);
  };

  return (
    <div className="flex min-w-0 max-w-full items-center gap-1" data-testid="intelligence-selector">
      <DropdownMenu>
        <DropdownMenuTrigger asChild disabled={disabled}>
          <Button
            variant="outline"
            size="sm"
            className="h-8 min-w-0 max-w-full justify-between gap-2 text-sm font-normal sm:min-w-[110px]"
            aria-label="Intelligence"
            data-testid="intelligence-selector-trigger"
            title={
              selectedTierId === null
                ? `${activeLabel} is this profile's default intelligence`
                : `${activeLabel} stays selected for this conversation`
            }
          >
            <span className="flex min-w-0 items-center gap-2">
              <Sparkles className="w-4 h-4 shrink-0" />
              <span className="truncate">{activeLabel}</span>
            </span>
            <ChevronDown className="w-4 h-4 shrink-0 opacity-50" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuRadioGroup value={value} onValueChange={handleChange}>
            {tiers.map((tier) => (
              <DropdownMenuRadioItem key={tier.id} value={tier.id}>
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
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          {presets.length > 0 && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuSub>
                <DropdownMenuSubTrigger data-testid="intelligence-specific-model">
                  <span>Specific model</span>
                  {activePreset && (
                    <span className="text-xs text-muted-foreground">{activePreset.label}</span>
                  )}
                </DropdownMenuSubTrigger>
                <DropdownMenuSubContent className="max-h-[60vh] overflow-y-auto">
                  <DropdownMenuRadioGroup value={value} onValueChange={handleChange}>
                    {presets.map((preset) => (
                      <DropdownMenuRadioItem key={preset.id} value={preset.id}>
                        <div className="flex flex-col gap-1 py-1">
                          <span className="font-medium">{preset.label}</span>
                          {preset.description && (
                            <div className="text-xs text-muted-foreground max-w-[250px]">
                              {preset.description}
                            </div>
                          )}
                        </div>
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuSubContent>
              </DropdownMenuSub>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
};

export default IntelligenceSelector;
