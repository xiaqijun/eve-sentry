import { Button } from "@arco-design/web-react";
import { MoonStar, Sun } from "lucide-react";

import { useTheme } from "./ThemeContext";

export function ThemeToggle({ className = "" }: { className?: string }) {
  const { theme, toggleTheme } = useTheme();
  const dark = theme === "dark";
  const label = dark ? "切换明亮模式" : "切换暗黑模式";

  return (
    <Button
      aria-label={label}
      aria-pressed={dark}
      className={`global-theme-toggle ${className}`.trim()}
      icon={dark ? <Sun aria-hidden size={15} /> : <MoonStar aria-hidden size={15} />}
      title={label}
      type="outline"
      onClick={toggleTheme}
    >
      <span>{dark ? "明亮" : "暗黑"}</span>
    </Button>
  );
}
