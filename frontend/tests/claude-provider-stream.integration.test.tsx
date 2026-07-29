// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import {
  ClaudeApiRetryRow,
  ClaudeInitRow,
  ClaudeRateLimitChrome,
  type SessionUiState,
} from "../src/session";
import type { SessionEvent } from "../src/api";

function event(overrides: Partial<SessionEvent>): SessionEvent {
  return {
    id: 1,
    kind: "assistant",
    ts: null,
    text: "",
    disposition: "rendered",
    ...overrides,
  };
}

function uiState(): SessionUiState {
  return { booleans: new Map() };
}

afterEach(() => cleanup());

describe("Claude provider stream renderers", () => {
  test("init chip starts collapsed and reveals details on click", () => {
    const { container } = render(
      <ClaudeInitRow
        event={event({
          kind: "claude_init",
          claude_init: {
            model: "opus-4.7",
            cwd: "/Users/henry/me/fun/wiki",
            claude_code_version: "1.0.0",
          },
        })}
        stateKey="init:1"
        uiState={uiState()}
      />,
    );

    const button = screen.getByRole("button", { name: /session started: opus-4\.7/i });
    const details = container.querySelector(".session-claude-init-collapsible");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(details?.classList.contains("is-open")).toBe(false);

    fireEvent.click(button);

    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(details?.classList.contains("is-open")).toBe(true);
    expect(screen.getByText("claude code version")).toBeTruthy();
  });

  test("api retry is visible in the main transcript chrome", () => {
    render(
      <ClaudeApiRetryRow
        event={event({
          kind: "claude_api_retry",
          claude_api_retry: {
            attempt: 2,
            max_retries: 3,
            error_status: "529",
            retry_delay_ms: 500,
          },
        })}
      />,
    );

    expect(screen.getByTestId("claude-api-retry-chip").textContent).toContain("retry 2/3");
    expect(screen.queryByTestId("session-provider-inspector")).toBeNull();
  });

  test("rate limit is hoisted only when status is not allowed", () => {
    const { rerender } = render(
      <ClaudeRateLimitChrome
        rate={{
          status: "rejected",
          rateLimitType: "five_hour",
          isUsingOverage: false,
          overageStatus: "rejected",
          overageDisabledReason: "out_of_credits",
          resetsAt: 1_784_910_600,
        }}
      />,
    );

    expect(screen.getByTestId("claude-rate-limit-session-pill")).toBeTruthy();
    expect(screen.getByText("rejected")).toBeTruthy();
    expect(screen.getByText("five_hour")).toBeTruthy();
    expect(screen.getByText("overage off")).toBeTruthy();
    expect(screen.getByText("overage status rejected")).toBeTruthy();
    expect(screen.getByText("overage reason out_of_credits")).toBeTruthy();

    rerender(<ClaudeRateLimitChrome rate={{ status: "allowed" }} />);
    expect(screen.queryByTestId("claude-rate-limit-session-pill")).toBeNull();
  });
});
