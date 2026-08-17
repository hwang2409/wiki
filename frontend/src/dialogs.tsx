import { useId } from "react";
import type { FormEventHandler, ReactNode } from "react";
import { X } from "lucide-react";
import { useModalA11y } from "./modal-a11y";

// WIKI-308: shared bb-parity Dialog shape used by every wiki modal
// (settings, spawn worker, spawn orchestrator, replace agent, destructive
// confirm). Bakes in the a11y contract — aria-modal, aria-labelledby,
// focus trap, focus restore, Escape + backdrop close — so callers only
// pass content. Two roles: "dialog" (informational / form submit) and
// "alertdialog" (destructive confirm). One size per surface via the
// `size` prop; the scrim, panel radius, sticky footer, and close X are
// the same everywhere.

export type DialogSize = "sm" | "md" | "lg";

interface CommonDialogProps {
  className?: string;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  size?: DialogSize;
  role?: "dialog" | "alertdialog";
  /** Non-header content — top of the panel body. */
  children?: ReactNode;
  /** Footer nodes (usually the primary + cancel buttons). */
  footer?: ReactNode;
  /** Left rail rendered inline with the body (settings modal only). */
  rail?: ReactNode;
  closeLabel?: string;
  /** When true, backdrop clicks + Escape are ignored (submitting). */
  busy?: boolean;
  /** Extra class merged onto the panel. */
  panelClassName?: string;
}

interface DialogAsDivProps extends CommonDialogProps {
  as?: "div";
  onSubmit?: never;
}

interface DialogAsFormProps extends CommonDialogProps {
  as: "form";
  onSubmit: FormEventHandler<HTMLFormElement>;
}

export type BbDialogProps = DialogAsDivProps | DialogAsFormProps;

function dialogSizeClass(size: DialogSize): string {
  switch (size) {
    case "sm":
      return "bb-dialog--sm";
    case "lg":
      return "bb-dialog--lg";
    default:
      return "bb-dialog--md";
  }
}

export function BbDialog(props: BbDialogProps) {
  const {
    title,
    description,
    size = "md",
    role = "dialog",
    children,
    footer,
    rail,
    closeLabel = "Close",
    busy = false,
    panelClassName,
    className,
    onClose,
  } = props;

  function requestClose() {
    if (busy) return;
    onClose();
  }

  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useModalA11y<HTMLElement>(true, requestClose);

  const panelClasses = ["bb-dialog", dialogSizeClass(size)];
  if (rail) panelClasses.push("bb-dialog--has-rail");
  if (panelClassName) panelClasses.push(panelClassName);
  if (className) panelClasses.push(className);

  const panelClass = panelClasses.join(" ");
  const attachDialogRef = (element: HTMLElement | null) => {
    dialogRef.current = element;
  };

  const header = (
    <header className="bb-dialog__header">
      <div className="bb-dialog__heading">
        <h2 className="bb-dialog__title" id={titleId}>
          {title}
        </h2>
        {description ? (
          <p className="bb-dialog__description" id={descriptionId}>
            {description}
          </p>
        ) : null}
      </div>
      <button
        aria-label={closeLabel}
        className="bb-dialog__close"
        disabled={busy}
        type="button"
        onClick={requestClose}
      >
        <X aria-hidden size={14} />
      </button>
    </header>
  );

  const body = rail ? (
    <div className="bb-dialog__split">
      <aside className="bb-dialog__rail" role="tablist" aria-label="Sections">
        {rail}
      </aside>
      <div className="bb-dialog__body">{children}</div>
    </div>
  ) : (
    <div className="bb-dialog__body">{children}</div>
  );

  const footerRow = footer ? (
    <footer className="bb-dialog__footer">{footer}</footer>
  ) : null;

  if (props.as === "form") {
    const { onSubmit } = props;
    return (
      <>
        <div
          aria-hidden="true"
          className="bb-dialog-scrim"
          onClick={requestClose}
        />
        <form
          ref={attachDialogRef}
          role={role}
          aria-modal="true"
          aria-labelledby={titleId}
          aria-describedby={description ? descriptionId : undefined}
          tabIndex={-1}
          className={panelClass}
          onSubmit={onSubmit}
          onClick={(event) => event.stopPropagation()}
        >
          {header}
          {body}
          {footerRow}
        </form>
      </>
    );
  }

  return (
    <>
      <div
        aria-hidden="true"
        className="bb-dialog-scrim"
        onClick={requestClose}
      />
      <div
        ref={attachDialogRef}
        role={role}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={panelClass}
        onClick={(event) => event.stopPropagation()}
      >
        {header}
        {body}
        {footerRow}
      </div>
    </>
  );
}

interface DialogRailItem {
  id: string;
  label: string;
  count?: number;
}

export interface DialogRailProps {
  items: DialogRailItem[];
  active: string;
  onSelect: (id: string) => void;
}

export function DialogRail({ items, active, onSelect }: DialogRailProps) {
  return (
    <>
      {items.map((item) => {
        const isActive = item.id === active;
        return (
          <button
            key={item.id}
            id={`bb-rail-${item.id}`}
            aria-controls={`bb-rail-panel-${item.id}`}
            aria-selected={isActive}
            role="tab"
            tabIndex={isActive ? 0 : -1}
            type="button"
            className={`bb-dialog__rail-item${isActive ? " is-active" : ""}`}
            onClick={() => onSelect(item.id)}
          >
            <span className="bb-dialog__rail-label">{item.label}</span>
            {item.count !== undefined ? (
              <span className="bb-dialog__rail-count">{item.count}</span>
            ) : null}
          </button>
        );
      })}
    </>
  );
}
