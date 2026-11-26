import type { ComponentProps } from "react";
import { Tooltip } from "radix-ui";
import { Button } from "./button";

type IconActionProps = Omit<
  ComponentProps<typeof Button>,
  "size" | "aria-label"
> & {
  label: string;
};

export function IconAction({ label, children, ...props }: IconActionProps) {
  return (
    <Tooltip.Provider delayDuration={250}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <Button variant="outline" {...props} size="icon" aria-label={label}>
            {children}
          </Button>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content
            className="icon-tooltip"
            sideOffset={8}
            collisionPadding={12}
          >
            {label}
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}
