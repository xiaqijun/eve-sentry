import { ChevronDown, Eye, EyeOff } from "lucide-react";
import { Popover, Switch as SwitchPrimitive } from "radix-ui";
import { cloneElement, createContext, useContext, useId, useState } from "react";
import type { ComponentProps, ReactElement, ReactNode } from "react";
import { cn } from "../../lib/utils";
import { SearchInput as SeatSearchInput } from "./seat/search-input";

type InputProps = Omit<ComponentProps<"input">, "onChange"> & {
  onChange?: (value: string) => void;
  placeholder?: string;
  allowClear?: boolean;
};

function TextInput({ className, onChange, allowClear, ...props }: InputProps) {
  return <input {...props} className={cn("ui-input", className)}
    onChange={onChange ? (event) => onChange(event.currentTarget.value) : undefined} />;
}

function Password({ className, ...props }: InputProps) {
  const [visible, setVisible] = useState(false);
  return <div className={cn("ui-input-inner-wrapper", className)}>
    <TextInput {...props} type={visible ? "text" : "password"} />
    <button type="button" disabled={props.disabled} aria-label={visible ? "隐藏密码" : "显示密码"} aria-pressed={visible}
      className="ui-input-action" onClick={() => setVisible((value) => !value)}>
      {visible ? <EyeOff size={16} /> : <Eye size={16} />}
    </button>
  </div>;
}

function SearchInput({ allowClear, value, onChange, className, ...props }: InputProps) {
  return <SeatSearchInput {...props} role="searchbox" className={cn("ui-input", className, !allowClear && "search-without-clear")}
    value={String(value ?? "")} onChange={(event) => onChange?.(event.currentTarget.value)} onClear={() => onChange?.("")} />;
}

export const Input = Object.assign(TextInput, { Password, Search: SearchInput });

export function Field({ label, children }: { label: string; children: ReactElement<{ id?: string }> }) {
  const fieldId = useId();
  const id = children.props.id ?? fieldId;
  return <div className="ui-form-item">
    <label className="ui-form-label-item" htmlFor={id}>{label}</label>
    {cloneElement(children, { id })}
  </div>;
}

export function Switch({ checked, onChange, className, ...props }: Omit<ComponentProps<typeof SwitchPrimitive.Root>, "onChange" | "onCheckedChange"> & {
  onChange?: (checked: boolean) => void;
}) {
  return <SwitchPrimitive.Root {...props} type="button" checked={checked} onCheckedChange={onChange} className={cn("ui-switch", className)}>
    <SwitchPrimitive.Thumb className="ui-switch-thumb" />
  </SwitchPrimitive.Root>;
}

export function MultiSelect({ id, label, value, onValueChange, options, disabled, placeholder }:
  { id?: string; label: string; value: number[]; onValueChange: (value: number[]) => void; options: Array<{ value: number; label: string }>;
    disabled?: boolean; placeholder: string; }) {
  const [search, setSearch] = useState("");
  const chosen = new Set(value);
  const available = new Map(options.map((option) => [option.value, option.label]));
  const visible = options.filter((option) => option.label.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  return <Popover.Root onOpenChange={(open) => { if (!open) setSearch(""); }}>
    <Popover.Trigger id={id} type="button" aria-label={label} disabled={disabled} className="ui-select-trigger w-full text-start">
      <span className="min-w-0 flex-1 truncate">
        {value.length ? value.slice(0, 3).map((item) => available.get(item) ?? item).join("、") : placeholder}
        {value.length > 3 ? ` +${value.length - 3}` : ""}
      </span><ChevronDown size={16} aria-hidden="true" />
    </Popover.Trigger>
    <Popover.Portal>
      <Popover.Content className="ui-select-content ui-multi-select" align="start" sideOffset={4} collisionPadding={8} aria-label={label}>
        <TextInput type="search" aria-label={`搜索${label}`} placeholder="搜索" value={search} onChange={setSearch} />
        <div className="ui-multi-options">
          {visible.map((option) => <label key={option.value} className="ui-select-option">
            <input type="checkbox" checked={chosen.has(option.value)} onChange={(event) => {
              onValueChange(event.currentTarget.checked ? [...value, option.value] : value.filter((item) => item !== option.value));
            }} />
            {option.label}
          </label>)}
          {!visible.length ? <p className="p-2 text-sm text-muted-foreground">没有匹配项</p> : null}
        </div>
        {value.length ? <button type="button" className="ui-multi-clear" onClick={() => onValueChange([])}>清空选择</button> : null}
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>;
}

type RadioValue = string | number;
const RadioContext = createContext<{ name: string; value: RadioValue; onChange: (value: RadioValue) => void; disabled?: boolean } | null>(null);

function RadioGroup<Value extends RadioValue>({ value, onChange, children, disabled, direction, type, "aria-label": label }:
  { value: Value; onChange: (value: Value) => void; children: ReactNode; disabled?: boolean; direction?: "vertical"; type?: "button"; "aria-label": string }) {
  const name = useId();
  return (
    <fieldset aria-label={label} className={cn("ui-radio-group", type === "button" && "ui-radio-buttons", direction === "vertical" && "flex-col")} disabled={disabled}>
      <RadioContext.Provider value={{ name, value, onChange: (next) => onChange(next as Value), disabled }}>{children}</RadioContext.Provider>
    </fieldset>
  );
}

function RadioOption({ value, disabled, children }: { value: RadioValue; disabled?: boolean; children: ReactNode }) {
  const group = useContext(RadioContext);
  if (!group) throw new Error("RadioOption requires a RadioGroup");
  return <label className="ui-radio">
    <input type="radio" name={group.name} value={value} checked={group.value === value}
      disabled={disabled || group.disabled} onChange={() => group.onChange(value)} />
    <span>{children}</span>
  </label>;
}

export const Radio = Object.assign(RadioOption, { Group: RadioGroup });
