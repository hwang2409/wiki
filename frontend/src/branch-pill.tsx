import { forwardRef } from "react";
import { GitBranch } from "lucide-react";

export type BranchPillProps = {
  branch: string;
  icon?: boolean;
  onClick?: () => void;
  className?: string;
  title?: string;
};

export const BranchPill = forwardRef<HTMLSpanElement, BranchPillProps>(function BranchPill(
  { branch, icon = true, onClick, className, title },
  ref,
) {
  const classes = ["branch-pill"];
  if (onClick) classes.push("is-clickable");
  if (className) classes.push(className);
  const tip = title ?? branch;
  return (
    <span
      className={classes.join(" ")}
      onClick={onClick}
      onKeyDown={
        onClick
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      ref={ref}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      title={tip}
    >
      {icon ? <GitBranch aria-hidden="true" className="branch-pill-icon" size={12} /> : null}
      <span className="branch-pill-label">{branch}</span>
    </span>
  );
});
