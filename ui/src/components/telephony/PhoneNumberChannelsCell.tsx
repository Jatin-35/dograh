"use client";

import { Check, Pencil, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  channelsOf,
  MAX_CHANNELS,
  MIN_CHANNELS,
  type PhoneNumberWithChannels,
  updatePhoneNumberChannels,
} from "@/lib/superadminPhoneNumbers";

/**
 * How many calls may run at once on a number. Everyone sees it; only a
 * superadmin can change it, because it reflects what the telephony provider
 * sold rather than a setting the organization controls.
 */
export function PhoneNumberChannelsCell({
  phoneNumber,
  canEdit,
  onSaved,
}: {
  phoneNumber: PhoneNumberWithChannels;
  canEdit: boolean;
  onSaved: (updated: PhoneNumberWithChannels) => void;
}) {
  const current = channelsOf(phoneNumber);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(String(current));
  const [saving, setSaving] = useState(false);

  const parsed = Number(value);
  const valid =
    Number.isInteger(parsed) && parsed >= MIN_CHANNELS && parsed <= MAX_CHANNELS;

  const startEditing = () => {
    setValue(String(current));
    setEditing(true);
  };

  const save = async () => {
    if (!valid) return;
    if (parsed === current) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      const updated = await updatePhoneNumberChannels(phoneNumber.id, parsed);
      onSaved(updated);
      setEditing(false);
      toast.success(`${phoneNumber.address} now allows ${parsed} concurrent calls`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to update channels");
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <div className="flex items-center gap-1">
        <span>{current}</span>
        {canEdit && (
          <Button
            variant="ghost"
            size="sm"
            onClick={startEditing}
            title="Change channels (superadmin)"
            aria-label={`Change channels for ${phoneNumber.address}`}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1">
      <Input
        type="number"
        min={MIN_CHANNELS}
        max={MAX_CHANNELS}
        step={1}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
          if (e.key === "Escape") setEditing(false);
        }}
        className="h-8 w-20"
        aria-label={`Channels for ${phoneNumber.address}`}
        aria-invalid={!valid}
        autoFocus
        disabled={saving}
      />
      <Button
        variant="ghost"
        size="sm"
        onClick={save}
        disabled={!valid || saving}
        title="Save"
        aria-label="Save channels"
      >
        <Check className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setEditing(false)}
        disabled={saving}
        title="Cancel"
        aria-label="Cancel"
      >
        <X className="h-4 w-4" />
      </Button>
    </div>
  );
}
