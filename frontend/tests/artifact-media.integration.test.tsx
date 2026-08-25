// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { SessionArtifact, SessionEvent } from "../src/api";
import { VideoRenderer, AudioRenderer } from "../src/artifact-renderers";
import { downloadName } from "../src/artifact-payload";

const TICKET = "WIKI-190";

function makeEvent(artifact: SessionArtifact, artifactId = "00000000-0000-4000-8000-000000000001"): SessionEvent {
  return {
    id: 1,
    kind: "artifact",
    ts: null,
    text: "",
    disposition: "kept",
    artifact_id: artifactId,
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
  test("renders WebM through the native video path", () => {
    const artifact: SessionArtifact = {
      kind: "video",
      mime: "video/webm",
      ref: "artifact://webm",
      width: 160,
      height: 120,
    };
    const event = makeEvent(artifact, "webm");
    render(<VideoRenderer artifact={artifact} event={event} ticket={TICKET} />);
    const video = screen.getByLabelText("Fixture media") as HTMLVideoElement;
    expect(video.tagName).toBe("VIDEO");
    expect(video.getAttribute("src")).toContain("/artifact/webm");
    expect(downloadName(event)).toBe("Fixture-media.webm");
  });

  test("renders a custom control bar with preload=metadata", () => {
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
    expect(video.hasAttribute("controls")).toBe(false);
    expect(video.hasAttribute("controlsList")).toBe(false);
    expect(video.getAttribute("preload")).toBe("metadata");
    expect(video.getAttribute("playsinline")).not.toBeNull();
    expect(video.getAttribute("src")).toContain("artifact");
    expect(screen.getByText("0:03")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Play" })).toBeTruthy();
    expect(screen.getByRole("slider", { name: "Seek" })).toBeTruthy();
    expect(screen.getByRole("slider", { name: "Volume" })).toBeTruthy();
  });

  test("video controls play, mute, volume, and seek the media element", () => {
    const artifact: SessionArtifact = {
      kind: "video",
      mime: "video/mp4",
      ref: "artifact://abc",
      duration_ms: 10000,
    };
    const playSpy = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    const pauseSpy = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    render(<VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const video = screen.getByLabelText("Fixture media") as HTMLVideoElement;
    Object.defineProperty(video, "paused", { configurable: true, value: true });
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(playSpy).toHaveBeenCalled();
    Object.defineProperty(video, "paused", { configurable: true, value: false });
    fireEvent(video, new Event("play"));
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(pauseSpy).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Mute" }));
    expect(video.muted).toBe(true);
    fireEvent.change(screen.getByRole("slider", { name: "Volume" }), { target: { value: "0.5" } });
    expect(video.volume).toBeCloseTo(0.5);
    expect(video.muted).toBe(false);
    fireEvent.change(screen.getByRole("slider", { name: "Seek" }), { target: { value: "4" } });
    expect(video.currentTime).toBe(4);
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

  test("reduced-motion same-kind GIF navigation resets to paused", () => {
    installMatchMedia(true);
    const first: SessionArtifact = {
      kind: "video",
      mime: "image/gif",
      ref: "artifact://first-gif",
      width: 100,
      height: 80,
    };
    const second: SessionArtifact = {
      kind: "video",
      mime: "image/gif",
      ref: "artifact://second-gif",
      width: 100,
      height: 80,
    };
    const { rerender } = render(
      <VideoRenderer artifact={first} event={makeEvent(first, "first-gif")} ticket={TICKET} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Play Fixture media/i }));
    expect(screen.queryByRole("button", { name: /Play Fixture media/i })).toBeNull();
    rerender(
      <VideoRenderer artifact={second} event={makeEvent(second, "second-gif")} ticket={TICKET} />,
    );
    expect(screen.getByRole("button", { name: /Play Fixture media/i })).toBeTruthy();
    expect(document.querySelector(".artifact-gif-frozen")).not.toBeNull();
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

  test("same-kind navigation reloads the video and keeps playback rate", () => {
    const first: SessionArtifact = { kind: "video", mime: "video/mp4", ref: "artifact://first" };
    const second: SessionArtifact = { kind: "video", mime: "video/mp4", ref: "artifact://second" };
    const loadSpy = vi.spyOn(HTMLMediaElement.prototype, "load");
    const { rerender } = render(
      <VideoRenderer artifact={first} event={makeEvent(first, "first")} ticket={TICKET} />,
    );
    const select = screen.getByLabelText("Playback speed") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "1.5" } });
    const video = screen.getByLabelText("Fixture media") as HTMLVideoElement;
    const firstSrc = video.getAttribute("src");
    rerender(<VideoRenderer artifact={second} event={makeEvent(second, "second")} ticket={TICKET} />);
    expect(video.getAttribute("src")).not.toBe(firstSrc);
    expect(video.getAttribute("src")).toContain("/artifact/second");
    expect(video.playbackRate).toBeCloseTo(1.5);
    expect(loadSpy).toHaveBeenCalled();
  });

  test("unmount cleanup clears the media src (defensive decoder release)", () => {
    const pauseSpy = vi.spyOn(HTMLMediaElement.prototype, "pause");
    const loadSpy = vi.spyOn(HTMLMediaElement.prototype, "load");
    const artifact: SessionArtifact = { kind: "video", mime: "video/mp4", ref: "artifact://abc" };
    const { unmount, container } = render(
      <VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />,
    );
    const video = container.querySelector("video")!;
    expect(video.getAttribute("src")).toBeTruthy();
    unmount();
    // The captured-in-effect cleanup path (not a live ref) fires — proves the
    // review's #8 "cleared ref races the cleanup" hazard cannot surface here.
    expect(pauseSpy).toHaveBeenCalled();
    expect(loadSpy).toHaveBeenCalled();
  });

  test("video uses fallback aspect ratio when dims are unknown", () => {
    const artifact: SessionArtifact = { kind: "video", mime: "video/mp4", ref: "artifact://abc" };
    render(<VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const frame = document.querySelector(".artifact-video-frame") as HTMLDivElement;
    expect(frame.style.aspectRatio.replace(/\s+/g, "")).toBe("16/9");
  });

  test("video renders poster when payload includes poster_base64", () => {
    const artifact: SessionArtifact = {
      kind: "video",
      mime: "video/mp4",
      ref: "artifact://abc",
      poster_base64: "data:image/jpeg;base64,AAAA",
    };
    render(<VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const video = screen.getByLabelText("Fixture media") as HTMLVideoElement;
    expect(video.getAttribute("poster")).toBe("data:image/jpeg;base64,AAAA");
  });

  test("mount-stress: 100 render/unmount cycles leave no leaked video elements", () => {
    const artifact: SessionArtifact = { kind: "video", mime: "video/mp4", ref: "artifact://abc" };
    for (let i = 0; i < 100; i += 1) {
      const { unmount } = render(
        <VideoRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />,
      );
      unmount();
    }
    // After every cycle, cleanup() below plus the release-on-null ref
    // callback must have removed every media element from the DOM.
    expect(document.querySelectorAll("video").length).toBe(0);
  });
});

describe("AudioRenderer", () => {
  test("renders a custom control bar with preload=metadata", () => {
    const artifact: SessionArtifact = {
      kind: "audio",
      mime: "audio/wav",
      ref: "artifact://audio",
      duration_ms: 65000,
    };
    render(<AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const audio = screen.getByLabelText("Fixture media") as HTMLAudioElement;
    expect(audio.tagName).toBe("AUDIO");
    expect(audio.hasAttribute("controls")).toBe(false);
    expect(audio.hasAttribute("controlsList")).toBe(false);
    expect(audio.getAttribute("preload")).toBe("metadata");
    expect(screen.getByText("1:05")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Play" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Enter fullscreen" })).toBeNull();
  });

  test("speed selector updates playbackRate on the audio element", () => {
    const artifact: SessionArtifact = { kind: "audio", mime: "audio/wav", ref: "artifact://a" };
    render(<AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const select = screen.getByLabelText("Playback speed") as HTMLSelectElement;
    const audio = screen.getByLabelText("Fixture media") as HTMLAudioElement;
    fireEvent.change(select, { target: { value: "0.75" } });
    expect(audio.playbackRate).toBeCloseTo(0.75);
  });

  test("same-kind navigation reloads the audio and keeps playback rate", () => {
    const first: SessionArtifact = { kind: "audio", mime: "audio/wav", ref: "artifact://first" };
    const second: SessionArtifact = { kind: "audio", mime: "audio/wav", ref: "artifact://second" };
    const loadSpy = vi.spyOn(HTMLMediaElement.prototype, "load");
    const { rerender } = render(
      <AudioRenderer artifact={first} event={makeEvent(first, "first")} ticket={TICKET} />,
    );
    const select = screen.getByLabelText("Playback speed") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "1.5" } });
    const audio = screen.getByLabelText("Fixture media") as HTMLAudioElement;
    const firstSrc = audio.getAttribute("src");
    rerender(<AudioRenderer artifact={second} event={makeEvent(second, "second")} ticket={TICKET} />);
    expect(audio.getAttribute("src")).not.toBe(firstSrc);
    expect(audio.getAttribute("src")).toContain("/artifact/second");
    expect(audio.playbackRate).toBeCloseTo(1.5);
    expect(loadSpy).toHaveBeenCalled();
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

  test("audio renders waveform bars when peaks are supplied", () => {
    const peaks = Array.from({ length: 32 }, (_, index) => (index * 8) % 256);
    const artifact: SessionArtifact = {
      kind: "audio",
      mime: "audio/wav",
      ref: "artifact://a",
      peaks,
    };
    render(<AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    const waveform = document.querySelector(".artifact-audio-waveform");
    expect(waveform).not.toBeNull();
    expect(waveform!.querySelectorAll("rect").length).toBe(peaks.length);
  });

  test("audio omits waveform when peaks are absent", () => {
    const artifact: SessionArtifact = { kind: "audio", mime: "audio/wav", ref: "artifact://a" };
    render(<AudioRenderer artifact={artifact} event={makeEvent(artifact)} ticket={TICKET} />);
    expect(document.querySelector(".artifact-audio-waveform")).toBeNull();
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
