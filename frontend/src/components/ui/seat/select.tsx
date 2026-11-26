import { msg } from "@/lib/i18n";
import { Select as Primitive } from "radix-ui";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import "./select.css";

export type SelectOption = {
  value: string;
  label: string;
  leading?: ReactNode;
  group?: string;
};

export function Select({
  label,
  value,
  onValueChange,
  options,
  placeholder = msg("请选择"),
  className,
  disabled,
}: {
  label: string;
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  className?: string;
  disabled?: boolean;
}) {
  const selected = options.find((option) => option.value === value);
  // Radix reserves the empty value for placeholders; business filters use it for "all".
  const encode = (raw: string) => `option:${raw}`;
  const groups = [...new Set(options.map((option) => option.group ?? ""))];
  return (
    <Primitive.Root
      disabled={disabled}
      value={selected ? encode(value) : ""}
      onValueChange={(next) => onValueChange(next.slice(7))}
    >
      <Primitive.Trigger
        aria-label={label}
        className={cn("ui-select-trigger", className)}
      >
        <span className="ui-select-value">
          {selected?.leading}
          <Primitive.Value placeholder={placeholder}>
            {selected?.label}
          </Primitive.Value>
        </span>
        <Primitive.Icon asChild>
          <ChevronDown
            className="ui-select-chevron"
            size={16}
            aria-hidden="true"
          />
        </Primitive.Icon>
      </Primitive.Trigger>
      <Primitive.Portal>
        <Primitive.Content
          className="ui-select-content"
          position="popper"
          align="start"
          sideOffset={6}
          collisionPadding={12}
        >
          <Primitive.ScrollUpButton className="ui-select-scroll">
            <ChevronUp size={16} />
          </Primitive.ScrollUpButton>
          <Primitive.Viewport className="ui-select-viewport">
            {groups.map((group) => (
              <Primitive.Group key={group}>
                {group && (
                  <Primitive.Label className="ui-select-group-label">
                    {group}
                  </Primitive.Label>
                )}
                {options
                  .filter((option) => (option.group ?? "") === group)
                  .map((option) => (
                    <Primitive.Item
                      key={option.value}
                      value={encode(option.value)}
                      textValue={option.label}
                      className="ui-select-item"
                    >
                      {option.leading}
                      <Primitive.ItemText>{option.label}</Primitive.ItemText>
                      <Primitive.ItemIndicator className="ui-select-check">
                        <Check size={16} aria-hidden="true" />
                      </Primitive.ItemIndicator>
                    </Primitive.Item>
                  ))}
              </Primitive.Group>
            ))}
          </Primitive.Viewport>
          <Primitive.ScrollDownButton className="ui-select-scroll">
            <ChevronDown size={16} />
          </Primitive.ScrollDownButton>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
