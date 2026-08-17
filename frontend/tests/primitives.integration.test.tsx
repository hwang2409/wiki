// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import {
  Button,
  Chip,
  DetailCard,
  DetailRow,
  IconButton,
  MenuRow,
  TabPill
} from "../src/primitives";
import { PrimitivesDemo } from "../src/primitives-demo";

afterEach(() => {
  cleanup();
});

describe("primitives — Button", () => {
  test("renders every variant × size and applies bb-parity classes", () => {
    const variants = ["default", "secondary", "outline", "ghost", "destructive"] as const;
    const sizes = ["sm", "default", "lg"] as const;
    render(
      <div>
        {variants.map((variant) =>
          sizes.map((size) => (
            <Button key={`${variant}-${size}`} variant={variant} size={size}>
              {variant}-{size}
            </Button>
          ))
        )}
      </div>
    );
    for (const variant of variants) {
      for (const size of sizes) {
        const button = screen.getByRole("button", { name: `${variant}-${size}` });
        expect(button.className).toContain(`bb-button--${variant}`);
        expect(button.className).toContain(
          size === "sm" ? "bb-button--sm" : size === "default" ? "bb-button--md" : "bb-button--lg"
        );
      }
    }
  });

  test("fires onClick and skips when disabled", () => {
    const onClick = vi.fn();
    render(
      <>
        <Button onClick={onClick}>go</Button>
        <Button onClick={onClick} disabled>
          nope
        </Button>
      </>
    );
    fireEvent.click(screen.getByRole("button", { name: "go" }));
    fireEvent.click(screen.getByRole("button", { name: "nope" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe("primitives — IconButton", () => {
  test("requires aria-label, honors pressed and disabled", () => {
    render(
      <>
        <IconButton aria-label="search" pressed>
          <svg data-testid="glyph" />
        </IconButton>
        <IconButton aria-label="delete" disabled>
          <svg />
        </IconButton>
      </>
    );
    const pressed = screen.getByRole("button", { name: "search" });
    expect(pressed.getAttribute("aria-pressed")).toBe("true");
    expect(pressed.className).toContain("bb-icon-button");
    expect(within(pressed).getByTestId("glyph")).toBeTruthy();

    const disabled = screen.getByRole("button", { name: "delete" });
    expect((disabled as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("primitives — TabPill", () => {
  test("renders label, marks active, and calls onSelect + onClose", () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(
      <TabPill
        label="Thread"
        isActive
        secondaryLabel="3"
        onSelect={onSelect}
        onClose={onClose}
      />
    );
    const selectButton = screen.getByRole("button", { name: "Thread3" });
    expect(selectButton.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(selectButton);
    expect(onSelect).toHaveBeenCalledTimes(1);

    const closeButton = screen.getByRole("button", { name: "Close Thread" });
    fireEvent.click(closeButton);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  test("omits close button when onClose is absent", () => {
    render(<TabPill label="Diff" />);
    expect(screen.queryByRole("button", { name: /Close Diff/ })).toBeNull();
  });
});

describe("primitives — Chip", () => {
  test("applies tone class and optional dot", () => {
    render(
      <>
        <Chip>plain</Chip>
        <Chip tone="danger" leadingDot>
          hot
        </Chip>
      </>
    );
    const plain = screen.getByText("plain");
    expect(plain.className).toContain("bb-chip--neutral");
    const hot = screen.getByText("hot");
    expect(hot.className).toContain("bb-chip--danger");
    expect(hot.querySelector(".bb-chip__dot")).toBeTruthy();
  });
});

describe("primitives — DetailCard", () => {
  test("renders label/value rows with shared column width", () => {
    render(
      <DetailCard labelWidth="120px">
        <DetailRow label="Ticket">WIKI-297</DetailRow>
        <DetailRow label="Files">4</DetailRow>
      </DetailCard>
    );
    expect(screen.getByText("WIKI-297")).toBeTruthy();
    expect(screen.getByText("Files")).toBeTruthy();
    const card = screen.getByText("WIKI-297").closest(".bb-detail-card");
    expect(card).not.toBeNull();
    expect((card as HTMLElement).style.getPropertyValue("--bb-detail-label-width")).toBe("120px");
  });
});

describe("primitives — MenuRow", () => {
  test("renders shortcut + destructive tone", () => {
    const onClick = vi.fn();
    render(
      <MenuRow leadingIcon={<svg data-testid="menu-glyph" />} shortcut="⌘K" onClick={onClick}>
        Search
      </MenuRow>
    );
    const row = screen.getByRole("menuitem", { name: /Search/ });
    expect(row.querySelector(".bb-menu-row__shortcut")?.textContent).toBe("⌘K");
    fireEvent.click(row);
    expect(onClick).toHaveBeenCalledTimes(1);

    cleanup();
    render(
      <MenuRow destructive shortcut="⌫">
        Delete
      </MenuRow>
    );
    const destructive = screen.getByRole("menuitem", { name: /Delete/ });
    expect(destructive.className).toContain("bb-menu-row--destructive");
  });
});

describe("primitives-demo", () => {
  test("mounts and lists every primitive section", () => {
    render(<PrimitivesDemo />);
    expect(screen.getByTestId("primitives-demo")).toBeTruthy();
    for (const heading of [
      "Button",
      "Icon button",
      "Tab pill",
      "Chip",
      "Detail card",
      "Menu row"
    ]) {
      expect(screen.getByRole("heading", { name: heading, level: 2 })).toBeTruthy();
    }
    // Every button variant should have at least one instance rendered.
    for (const label of ["Save changes", "Cancel", "Connect repo", "Settings", "Delete project"]) {
      expect(screen.getAllByRole("button", { name: label }).length).toBeGreaterThan(0);
    }
    // Menu row shortcuts render.
    expect(screen.getByRole("menuitem", { name: /New thread/ })).toBeTruthy();
  });
});
