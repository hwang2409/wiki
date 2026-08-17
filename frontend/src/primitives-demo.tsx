import { useState } from "react";
import {
  ArrowRight,
  Check,
  ChevronDown,
  Command,
  Cog,
  FileText,
  MessageSquare,
  Plus,
  Search,
  Sparkles,
  Trash2
} from "lucide-react";

import {
  Button,
  Chip,
  DetailCard,
  DetailRow,
  IconButton,
  MenuRow,
  TabPill,
  type ButtonSize,
  type ButtonVariant
} from "./primitives";
import { toast, Toaster } from "./toast";

const BUTTON_VARIANTS: ButtonVariant[] = [
  "default",
  "secondary",
  "outline",
  "ghost",
  "destructive"
];
const BUTTON_SIZES: ButtonSize[] = ["sm", "default", "lg"];

const VARIANT_LABELS: Record<ButtonVariant, string> = {
  default: "Save changes",
  secondary: "Cancel",
  outline: "Connect repo",
  ghost: "Settings",
  destructive: "Delete project"
};

// WIKI-297: single mount point for a visual inventory of every primitive
// variant. Rendered inside the running app via `#/primitives-demo` in main.tsx,
// and rendered inside jsdom by tests/primitives.integration.test.tsx.
export function PrimitivesDemo() {
  const [activeTab, setActiveTab] = useState("thread");
  return (
    <div className="bb-primitives-demo" data-testid="primitives-demo">
      <header className="bb-primitives-demo__header">
        <h1 className="bb-primitives-demo__title">WIKI-297 primitives</h1>
        <p className="bb-primitives-demo__caption">
          bb-parity Button, IconButton, TabPill, Chip, DetailCard, MenuRow — rendered against wiki
          semantic tokens.
        </p>
      </header>

      <section className="bb-primitives-demo__section" aria-labelledby="primitives-buttons">
        <h2 className="bb-primitives-demo__section-title" id="primitives-buttons">
          Button
        </h2>
        <div className="bb-primitives-demo__grid">
          {BUTTON_VARIANTS.map((variant) => (
            <div className="bb-primitives-demo__row" key={variant}>
              <div className="bb-primitives-demo__row-label">{variant}</div>
              <div className="bb-primitives-demo__row-content">
                {BUTTON_SIZES.map((size) => (
                  <Button key={size} variant={variant} size={size}>
                    {VARIANT_LABELS[variant]}
                  </Button>
                ))}
                <Button variant={variant} disabled>
                  disabled
                </Button>
              </div>
            </div>
          ))}
          <div className="bb-primitives-demo__row">
            <div className="bb-primitives-demo__row-label">with icons</div>
            <div className="bb-primitives-demo__row-content">
              <Button leadingIcon={<Check aria-hidden />}>Save changes</Button>
              <Button variant="outline" size="sm" trailingIcon={<ArrowRight aria-hidden />}>
                Add local path
              </Button>
              <Button variant="ghost" leadingIcon={<Sparkles aria-hidden />}>
                New thread
              </Button>
            </div>
          </div>
        </div>
      </section>

      <section className="bb-primitives-demo__section" aria-labelledby="primitives-icon-buttons">
        <h2 className="bb-primitives-demo__section-title" id="primitives-icon-buttons">
          Icon button
        </h2>
        <div className="bb-primitives-demo__row-content">
          <IconButton aria-label="Search">
            <Search aria-hidden />
          </IconButton>
          <IconButton aria-label="New thread">
            <Plus aria-hidden />
          </IconButton>
          <IconButton aria-label="Command palette" pressed>
            <Command aria-hidden />
          </IconButton>
          <IconButton aria-label="Settings" variant="outline">
            <Cog aria-hidden />
          </IconButton>
          <IconButton aria-label="Disabled" disabled>
            <Trash2 aria-hidden />
          </IconButton>
        </div>
      </section>

      <section className="bb-primitives-demo__section" aria-labelledby="primitives-tab-pills">
        <h2 className="bb-primitives-demo__section-title" id="primitives-tab-pills">
          Tab pill
        </h2>
        <div className="bb-primitives-demo__row-content" role="tablist">
          <TabPill
            label="Thread"
            leadingVisual={<MessageSquare aria-hidden />}
            isActive={activeTab === "thread"}
            onSelect={() => setActiveTab("thread")}
            onClose={() => undefined}
          />
          <TabPill
            label="Docs"
            leadingVisual={<FileText aria-hidden />}
            isActive={activeTab === "docs"}
            secondaryLabel="12"
            onSelect={() => setActiveTab("docs")}
            onClose={() => undefined}
          />
          <TabPill
            label="Runs"
            isActive={activeTab === "runs"}
            onSelect={() => setActiveTab("runs")}
          />
          <TabPill label="Diff" onSelect={() => undefined} onClose={() => undefined} />
        </div>
      </section>

      <section className="bb-primitives-demo__section" aria-labelledby="primitives-chips">
        <h2 className="bb-primitives-demo__section-title" id="primitives-chips">
          Chip
        </h2>
        <div className="bb-primitives-demo__row-content">
          <Chip>Draft</Chip>
          <Chip tone="accent" leadingDot>
            Active
          </Chip>
          <Chip tone="warning" leadingDot>
            Attention
          </Chip>
          <Chip tone="danger" leadingDot>
            Blocked
          </Chip>
          <Chip tone="success" leadingDot>
            Merged
          </Chip>
        </div>
      </section>

      <section className="bb-primitives-demo__section" aria-labelledby="primitives-detail-card">
        <h2 className="bb-primitives-demo__section-title" id="primitives-detail-card">
          Detail card
        </h2>
        <div className="bb-primitives-demo__cards">
          <DetailCard>
            <DetailRow label="Ticket">WIKI-297</DetailRow>
            <DetailRow label="Owner">Claude Opus</DetailRow>
            <DetailRow label="State">
              <Chip tone="accent" leadingDot>
                In review
              </Chip>
            </DetailRow>
          </DetailCard>
          <DetailCard appearance="flat">
            <DetailRow label="Branch" orientation="vertical">
              <code className="bb-primitives-demo__code">wiki-297-primitives</code>
            </DetailRow>
            <DetailRow label="Commits">3</DetailRow>
            <DetailRow label="Files changed">4</DetailRow>
          </DetailCard>
        </div>
      </section>

      <section className="bb-primitives-demo__section" aria-labelledby="primitives-menu">
        <h2 className="bb-primitives-demo__section-title" id="primitives-menu">
          Menu row
        </h2>
        <div className="bb-primitives-demo__menu" role="menu" aria-label="Demo menu">
          <MenuRow leadingIcon={<Plus aria-hidden />} shortcut="⌘N">
            New thread
          </MenuRow>
          <MenuRow leadingIcon={<Search aria-hidden />} shortcut="⌘K">
            Search
          </MenuRow>
          <MenuRow
            leadingIcon={<ChevronDown aria-hidden />}
            shortcut="⌘⇧D"
            selected
          >
            Toggle disclosure
          </MenuRow>
          <div className="bb-primitives-demo__menu-divider" role="separator" />
          <MenuRow leadingIcon={<Trash2 aria-hidden />} destructive shortcut="⌫">
            Delete run
          </MenuRow>
        </div>
      </section>

      <section className="bb-primitives-demo__section" aria-labelledby="primitives-toast">
        <h2 className="bb-primitives-demo__section-title" id="primitives-toast">
          Toast
        </h2>
        <div className="bb-primitives-demo__row">
          <div className="bb-primitives-demo__row-label">trigger</div>
          <div className="bb-primitives-demo__row-content">
            <Button
              size="sm"
              variant="secondary"
              onClick={() => toast.message("Reminder", { description: "Something worth noticing" })}
            >
              Message
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() =>
                toast.success("Spawned PHO-15864", {
                  description: "run 8f4a1c2b · log /tmp/pho-15864.log",
                })
              }
            >
              Success
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() =>
                toast.warning("Codex limit approaching", {
                  description: "70 minutes of budget remaining across your accounts.",
                })
              }
            >
              Warning
            </Button>
            <Button
              size="sm"
              variant="destructive"
              onClick={() =>
                toast.error("Could not save note", {
                  description: "Backend returned 500 while writing vault/log/2026-08-17.md.",
                })
              }
            >
              Error
            </Button>
          </div>
        </div>
      </section>
      <Toaster />
    </div>
  );
}
