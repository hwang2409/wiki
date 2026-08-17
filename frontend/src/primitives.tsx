import { forwardRef } from "react";
import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";
import { X } from "lucide-react";

// WIKI-297: shared primitives that mirror bb's Button / icon-button /
// tab-pill / detail-card / menu-row grammar, but styled entirely through
// wiki semantic tokens. New surfaces should reach for these; existing
// surfaces migrate in WIKI-299+ waves — this ticket only introduces the
// primitives (F3 of the WIKI-294 parity program).

export type ButtonVariant =
  | "default"
  | "secondary"
  | "outline"
  | "ghost"
  | "destructive";
export type ButtonSize = "sm" | "default" | "lg";

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  default: "bb-button--default",
  secondary: "bb-button--secondary",
  outline: "bb-button--outline",
  ghost: "bb-button--ghost",
  destructive: "bb-button--destructive"
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: "bb-button--sm",
  default: "bb-button--md",
  lg: "bb-button--lg"
};

function joinClasses(...values: Array<string | false | null | undefined>): string {
  return values.filter((value): value is string => Boolean(value)).join(" ");
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  leadingIcon?: ReactNode;
  trailingIcon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "default",
    size = "default",
    leadingIcon,
    trailingIcon,
    className,
    type = "button",
    children,
    ...rest
  },
  ref
) {
  return (
    <button
      ref={ref}
      type={type}
      className={joinClasses("bb-button", VARIANT_CLASS[variant], SIZE_CLASS[size], className)}
      {...rest}
    >
      {leadingIcon ? <span className="bb-button__glyph">{leadingIcon}</span> : null}
      {children != null ? <span className="bb-button__label">{children}</span> : null}
      {trailingIcon ? <span className="bb-button__glyph">{trailingIcon}</span> : null}
    </button>
  );
});

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Screen-reader label. Required because icon buttons carry no visible text. */
  "aria-label": string;
  variant?: "ghost" | "outline";
  /** Pressed toggles the persistent selected fill (bb's aria-pressed pattern). */
  pressed?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { variant = "ghost", pressed, className, type = "button", children, ...rest },
  ref
) {
  return (
    <button
      ref={ref}
      type={type}
      aria-pressed={pressed}
      className={joinClasses(
        "bb-icon-button",
        variant === "outline" && "bb-icon-button--outline",
        className
      )}
      {...rest}
    >
      {children}
    </button>
  );
});

export interface TabPillProps
  extends Omit<HTMLAttributes<HTMLDivElement>, "onSelect" | "title"> {
  label: string;
  title?: string;
  isActive?: boolean;
  leadingVisual?: ReactNode;
  secondaryLabel?: ReactNode;
  onSelect?: () => void;
  onClose?: () => void;
  closeLabel?: string;
}

export function TabPill({
  label,
  title,
  isActive = false,
  leadingVisual,
  secondaryLabel,
  onSelect,
  onClose,
  closeLabel,
  className,
  ...rest
}: TabPillProps) {
  return (
    <div
      className={joinClasses(
        "bb-tab-pill",
        isActive && "bb-tab-pill--active",
        onClose && "bb-tab-pill--closable",
        className
      )}
      {...rest}
    >
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={isActive}
        className="bb-tab-pill__button"
      >
        {leadingVisual ? (
          <span className="bb-tab-pill__leading">{leadingVisual}</span>
        ) : null}
        <span className="bb-tab-pill__label" title={title ?? label}>
          {label}
        </span>
        {secondaryLabel ? (
          <span className="bb-tab-pill__secondary">{secondaryLabel}</span>
        ) : null}
      </button>
      {onClose ? (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onClose();
          }}
          aria-label={closeLabel ?? `Close ${label}`}
          className="bb-tab-pill__close"
        >
          <CloseGlyph />
        </button>
      ) : null}
    </div>
  );
}

export interface ChipProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: "neutral" | "accent" | "warning" | "danger" | "success";
  leadingDot?: boolean;
}

export const Chip = forwardRef<HTMLSpanElement, ChipProps>(function Chip(
  { tone = "neutral", leadingDot = false, className, children, ...rest },
  ref
) {
  return (
    <span
      ref={ref}
      className={joinClasses("bb-chip", `bb-chip--${tone}`, className)}
      {...rest}
    >
      {leadingDot ? <span className="bb-chip__dot" aria-hidden /> : null}
      {children}
    </span>
  );
});

export interface DetailCardProps extends HTMLAttributes<HTMLElement> {
  appearance?: "card" | "flat";
  labelWidth?: string;
  children: ReactNode;
}

export function DetailCard({
  appearance = "card",
  labelWidth,
  className,
  style,
  children,
  ...rest
}: DetailCardProps) {
  const mergedStyle = labelWidth
    ? ({ ...(style ?? {}), ["--bb-detail-label-width" as string]: labelWidth } as typeof style)
    : style;
  return (
    <dl
      className={joinClasses(
        "bb-detail-card",
        appearance === "flat" && "bb-detail-card--flat",
        className
      )}
      style={mergedStyle}
      {...rest}
    >
      {children}
    </dl>
  );
}

export interface DetailRowProps {
  label: ReactNode;
  children: ReactNode;
  className?: string;
  orientation?: "horizontal" | "vertical";
}

export function DetailRow({
  label,
  children,
  className,
  orientation = "horizontal"
}: DetailRowProps) {
  return (
    <div
      className={joinClasses(
        "bb-detail-row",
        orientation === "vertical" && "bb-detail-row--vertical",
        className
      )}
    >
      <dt className="bb-detail-row__label">{label}</dt>
      <dd className="bb-detail-row__value">{children}</dd>
    </div>
  );
}

export interface MenuRowProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  leadingIcon?: ReactNode;
  shortcut?: string;
  destructive?: boolean;
  selected?: boolean;
}

export const MenuRow = forwardRef<HTMLButtonElement, MenuRowProps>(function MenuRow(
  { leadingIcon, shortcut, destructive, selected, className, type = "button", children, ...rest },
  ref
) {
  return (
    <button
      ref={ref}
      type={type}
      role="menuitem"
      aria-current={selected ? "true" : undefined}
      className={joinClasses(
        "bb-menu-row",
        destructive && "bb-menu-row--destructive",
        selected && "bb-menu-row--selected",
        className
      )}
      {...rest}
    >
      {leadingIcon ? <span className="bb-menu-row__glyph">{leadingIcon}</span> : null}
      <span className="bb-menu-row__label">{children}</span>
      {shortcut ? <span className="bb-menu-row__shortcut">{shortcut}</span> : null}
    </button>
  );
});

function CloseGlyph() {
  return <X aria-hidden className="bb-tab-pill__close-glyph" size={14} />;
}
