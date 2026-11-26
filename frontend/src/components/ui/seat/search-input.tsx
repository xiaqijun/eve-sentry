import { useRef, type InputHTMLAttributes } from "react";
import { X } from "lucide-react";
import { msg } from "@/lib/i18n";
import "./search-input.css";

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "value"> & {
  value: string;
  onClear: () => void;
  clearable?: boolean;
};

export function SearchInput({
  value,
  onClear,
  clearable,
  className = "",
  onKeyDown,
  ...props
}: Props) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <div className="search-field">
      <input
        {...props}
        ref={input}
        type="text"
        className={`search-field-input ${className}`}
        value={value}
        onKeyDown={(event) => {
          // Confirming a Chinese IME candidate must not submit the surrounding form.
          if (
            event.key === "Enter" &&
            (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)
          ) {
            event.preventDefault();
            return;
          }
          onKeyDown?.(event);
        }}
      />
      {(value !== "" || clearable) && (
        <button
          type="button"
          className="search-field-clear"
          aria-label={msg("清除搜索")}
          onClick={() => {
            onClear();
            input.current?.focus();
          }}
        >
          <X size={16} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}
