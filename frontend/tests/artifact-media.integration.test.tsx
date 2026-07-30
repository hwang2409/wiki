// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { SessionArtifact, SessionEvent } from "../src/api";
import { VideoRenderer, AudioRenderer } from "../src/artifact-renderers";

const TICKET = "WIKI-190";

function makeEvent(artifact: SessionArtifact): SessionEvent {
  return {
    id: 1,
    kind: "artifact",
    ts: null,
    text: "",
    disposition: "kept",
    artifact_id: "00000000-0000-4000-8000-000000000001",
    title: "Fixture media",
    caption: "test",
    artifact,
  };
}

let matchesReducedMotion = false;

function installMatchMedia(reduce: boolean) {
  matchesReducedMotion = reduce;
  const listeners = new Set<() => void>();
  const mql = {
    matches: reduce,
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
    addListener: (listener: () => void) => listeners.add(listener),
    removeListener: (listener: () => void) => listeners.delete(listener),
    dispatchEvent: () => true,
  };
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: () => mql,
  });
}

beforeEach(() => {
  installMatchMedia(false);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("VideoRenderer", () => {
  test("renders a native <video> with controls and preload=metadata", () => {
    const artifact: SessionArtifact = {
      kind: "video",
      mime: "video/mp4",
      ref: "artifact://abc",
      byte_size: 4096,
      duration_ms: 2500,
      width: 320,
      height: 240,
    };
    render(<VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const video = screen.getByLabelText("Fixture media") as HTMLVideoElement;
    expect(video.tagName).toBe("VIDEO");
    expect(video.hasAttribute("controls")).toBe(true);
    expect(video.getAttribute("preload")).toBe("metadata");
    expect(video.getAttribute("playsinline")).not.toBeNull();
    const source = video.querySelector("source");
    expect(source?.getAttribute("type")).toBe("video/mp4");
    expect(screen.getByText("0:03")).toBeTruthy();
  });

  test("reserves aspect ratio to prevent CLS when width/height are known", () => {
    const artifact: SessionArtifact = {
      kind: "video",
      mime: "video/mp4",
      ref: "artifact://abc",
      width: 640,
      height: 480,
    };
    render(<VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const frame = document.querySelector(".artifact-video-frame") as HTMLDivElement;
    expect(frame.style.aspectRatio).toBe("640 / 480");
  });

  test("speed selector updates playbackRate on the video element", () => {
    const artifact: SessionArtifact = { kind: "video", mime: "video/mp4", ref: "artifact://abc" };
    render(<VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const select = screen.getByLabelText("Playback speed") as HTMLSelectElement;
    const video = screen.getByLabelText("Fixture media") as HTMLVideoElement;
    fireEvent.change(select, { target: { value: "1.5" } });
    expect(video.playbackRate).toBeCloseTo(1.5);
  });

  test("gif with reduced-motion renders paused affordance", () => {
    installMatchMedia(true);
    const artifact: SessionArtifact = {
      kind: "video",
      mime: "image/gif",
      ref: "artifact://gif",
      width: 100,
      height: 80,
    };
    render(<VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    expect(screen.getByRole("button", { name: /Play Fixture media/i })).toBeTruthy();
    const frozen = document.querySelector(".artifact-gif-frozen");
    expect(frozen).not.toBeNull();
  });

  test("gif without reduced-motion plays inline (no play affordance)", () => {
    installMatchMedia(false);
    const artifact: SessionArtifact = {
      kind: "video",
      mime: "image/gif",
      ref: "artifact://gif",
    };
    render(<VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    expect(screen.queryByRole("button", { name: /Play/i })).toBeNull();
  });

  test("unmount removes the video element from the DOM", () => {
    const artifact: SessionArtifact = { kind: "video", mime: "video/mp4", ref: "artifact://abc" };
    const { unmount } = render(
      <VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />,
    );
    expect(screen.queryByLabelText("Fixture media")).not.toBeNull();
    unmount();
    expect(screen.queryByLabelText("Fixture media")).toBeNull();
  });
});

describe("AudioRenderer", () => {
  test("renders a native <audio> element with controls and preload=metadata", () => {
    const artifact: SessionArtifact = {
      kind: "audio",
      mime: "audio/wav",
      ref: "artifact://audio",
      duration_ms: 65000,
    };
    render(<AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const audio = screen.getByLabelText("Fixture media") as HTMLAudioElement;
    expect(audio.tagName).toBe("AUDIO");
    expect(audio.hasAttribute("controls")).toBe(true);
    expect(audio.getAttribute("preload")).toBe("metadata");
    expect(screen.getByText("1:05")).toBeTruthy();
  });

  test("speed selector updates playbackRate on the audio element", () => {
    const artifact: SessionArtifact = { kind: "audio", mime: "audio/wav", ref: "artifact://a" };
    render(<AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const select = screen.getByLabelText("Playback speed") as HTMLSelectElement;
    const audio = screen.getByLabelText("Fixture media") as HTMLAudioElement;
    fireEvent.change(select, { target: { value: "0.75" } });
    expect(audio.playbackRate).toBeCloseTo(0.75);
  });

  test("transcript toggle reveals and hides the transcript block", () => {
    const artifact: SessionArtifact = {
      kind: "audio",
      mime: "audio/wav",
      ref: "artifact://a",
      transcript: "hello world from a test transcript",
    };
    render(<AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    expect(screen.queryByText("hello world from a test transcript")).toBeNull();
    const toggle = screen.getByRole("button", { name: /Show transcript/i });
    fireEvent.click(toggle);
    expect(screen.getByRole("region", { name: "Transcript" })).toBeTruthy();
    expect(screen.getByText("hello world from a test transcript")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Hide transcript/i }));
    expect(screen.queryByText("hello world from a test transcript")).toBeNull();
  });

  test("no transcript toggle when transcript is absent", () => {
    const artifact: SessionArtifact = { kind: "audio", mime: "audio/wav", ref: "artifact://a" };
    render(<AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    expect(screen.queryByRole("button", { name: /transcript/i })).toBeNull();
  });

  test("unmount removes the audio element from the DOM", () => {
    const artifact: SessionArtifact = { kind: "audio", mime: "audio/wav", ref: "artifact://a" };
    const { unmount } = render(
      <AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />,
    );
    expect(screen.queryByLabelText("Fixture media")).not.toBeNull();
    unmount();
    expect(screen.queryByLabelText("Fixture media")).toBeNull();
  });
});

// Silence unused-variable warning for reduce state fixture.
void matchesReducedMotion;
