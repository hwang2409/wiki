import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Play } from "lucide-react";
import type { SessionArtifact, SessionEvent } from "./api";
import { artifactUrl, type ArtifactRendererProps } from "./artifact-renderers-shared";
import { MediaControls } from "./artifact-media-controls";

// Fallback aspect ratio for videos whose containers do not carry stored
// dimensions (currently only image/gif when the payload omits width/height).
// Keeps the frame the same size before and after `loadedmetadata` so the
// transcript never jumps.
const VIDEO_FALLBACK_ASPECT_RATIO = "16 / 9";

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handler = () => setReduced(mq.matches);
    if (typeof mq.addEventListener === "function") {
      mq.addEventListener("change", handler);
      return () => mq.removeEventListener("change", handler);
    }
    mq.addListener(handler);
    return () => mq.removeListener(handler);
  }, []);
  return reduced;
}

function artifactSource(artifact: SessionArtifact, event: SessionEvent, ticket: string, fallback: string): string {
  if (artifact.data_base64) return `data:${artifact.mime ?? fallback};base64,${artifact.data_base64}`;
  return artifactUrl(ticket, event);
}

// Capture the node inside the ref callback and run the release path on the
// SAME callback invocation when React passes null on unmount. Going through
// useEffect cleanup would race the ref-clearing pass — the ref is nulled
// before the effect cleanup fires, so the effect would see nothing to
// release. Doing the work here also lets us clear every `<source>` child's
// URL, not just the parent's — some browsers keep the decoder attached to
// the last mounted <source>.
function useReleaseMediaOnUnmount<T extends HTMLMediaElement>(): (node: T | null) => void {
  const captured = useRef<T | null>(null);
  // Callback identity is pinned so React does not re-run the ref on every
  // render — a fresh callback each render would look like unmount + mount
  // to React, and the "unmount" pass would clear the <source> src while
  // the element is still on screen.
  return useCallback((node: T | null) => {
    if (node) {
      captured.current = node;
      return;
    }
    const previous = captured.current;
    if (!previous) return;
    captured.current = null;
    try {
      previous.pause();
    } catch {
      /* jsdom / detached element */
    }
    const sources = previous.querySelectorAll("source");
    sources.forEach((source) => {
      source.removeAttribute("src");
      source.removeAttribute("srcset");
    });
    previous.removeAttribute("src");
    try {
      previous.load();
    } catch {
      /* jsdom / detached element */
    }
  }, []);
}

function GifRenderer({
  source,
  width,
  height,
  label,
}: {
  source: string;
  width?: number;
  height?: number;
  label: string;
}) {
  const reducedMotion = usePrefersReducedMotion();
  const [playing, setPlaying] = useState(!reducedMotion);
  useEffect(() => {
    setPlaying(!reducedMotion);
  }, [reducedMotion, source]);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  useEffect(() => {
    if (playing) return;
    const img = imgRef.current;
    const canvas = canvasRef.current;
    if (!img || !canvas) return;
    let cancelled = false;
    const draw = () => {
      if (cancelled) return;
      const naturalWidth = img.naturalWidth || width || 0;
      const naturalHeight = img.naturalHeight || height || 0;
      if (!naturalWidth || !naturalHeight) return;
      canvas.width = naturalWidth;
      canvas.height = naturalHeight;
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.drawImage(img, 0, 0);
    };
    if (img.complete && img.naturalWidth > 0) {
      draw();
    } else {
      img.addEventListener("load", draw, { once: true });
    }
    return () => {
      cancelled = true;
      img.removeEventListener("load", draw);
    };
  }, [playing, source, width, height]);
  const ratioStyle: CSSProperties = width && height
    ? { aspectRatio: `${width} / ${height}` }
    : { aspectRatio: VIDEO_FALLBACK_ASPECT_RATIO };
  return (
    <div
      className="artifact-video-wrap artifact-gif-wrap"
      style={{ ...ratioStyle, maxWidth: width ? `${width}px` : undefined }}
    >
      <img
        ref={imgRef}
        src={source}
        alt={label}
        className={`artifact-video${playing ? "" : " is-frozen-source"}`}
        aria-hidden={playing ? undefined : true}
        decoding="async"
        loading="lazy"
        width={width}
        height={height}
      />
      {playing ? null : (
        <>
          <canvas
            ref={canvasRef}
            className="artifact-video artifact-gif-frozen"
            aria-label={`${label} (paused)`}
            role="img"
          />
          <button
            className="artifact-gif-play"
            onClick={() => setPlaying(true)}
            type="button"
            aria-label={`Play ${label}`}
          >
            <Play size={16} aria-hidden="true" />
          </button>
        </>
      )}
    </div>
  );
}

export function VideoRenderer({ artifact, event, ticket }: ArtifactRendererProps) {
  const source = artifactSource(artifact, event, ticket, "video/mp4");
  const label = event.title || event.caption || "Video artifact";
  const releaseRef = useReleaseMediaOnUnmount<HTMLVideoElement>();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [speed, setSpeed] = useState(1);
  const speedRef = useRef(1);
  useEffect(() => {
    speedRef.current = speed;
    if (videoRef.current) videoRef.current.playbackRate = speed;
  }, [speed]);
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    try {
      video.load();
    } catch {
      /* jsdom / detached element */
    }
    video.playbackRate = speedRef.current;
  }, [source]);
  const attachVideoRef = useCallback(
    (node: HTMLVideoElement | null) => {
      videoRef.current = node;
      releaseRef(node);
    },
    [releaseRef],
  );
  if (artifact.mime === "image/gif") {
    return (
      <GifRenderer
        source={source}
        width={artifact.width}
        height={artifact.height}
        label={label}
      />
    );
  }
  const knownDims = artifact.width && artifact.height;
  const ratioStyle: CSSProperties = knownDims
    ? { aspectRatio: `${artifact.width} / ${artifact.height}` }
    : { aspectRatio: VIDEO_FALLBACK_ASPECT_RATIO };
  const durationSeconds = artifact.duration_ms
    ? artifact.duration_ms / 1000
    : undefined;
  const frameStyle: CSSProperties = {
    ...ratioStyle,
    maxWidth: artifact.width ? `${artifact.width}px` : undefined,
  };
  return (
    <div className="artifact-video-wrap">
      <MediaControls
        className="artifact-video-player"
        controlsClassName="artifact-video-controls"
        initialDuration={durationSeconds}
        mediaKey={source}
        mediaRef={videoRef}
        onSpeedChange={setSpeed}
        showFullscreen
        speed={speed}
      >
        <div className="artifact-video-frame" style={frameStyle}>
          <video
            ref={attachVideoRef}
            className="artifact-video"
            preload="metadata"
            playsInline
            poster={artifact.poster_base64}
            aria-label={label}
            src={source}
            width={artifact.width}
            height={artifact.height}
          />
        </div>
      </MediaControls>
    </div>
  );
}

function AudioWaveform({ peaks, label }: { peaks: number[]; label: string }) {
  const svg = useMemo(() => {
    if (!peaks.length) return null;
    // Center-line waveform: each peak draws a symmetric bar around the mid.
    const width = 100;
    const height = 40;
    const barWidth = width / peaks.length;
    const bars = peaks.map((peak, index) => {
      const normalized = Math.max(2, (peak / 255) * height);
      const y = (height - normalized) / 2;
      return { x: index * barWidth, y, w: Math.max(0.5, barWidth * 0.75), h: normalized };
    });
    return { width, height, bars };
  }, [peaks]);
  if (!svg) return null;
  return (
    <svg
      aria-label={`${label} waveform`}
      className="artifact-audio-waveform"
      preserveAspectRatio="none"
      role="img"
      viewBox={`0 0 ${svg.width} ${svg.height}`}
    >
      {svg.bars.map((bar, index) => (
        <rect key={index} x={bar.x} y={bar.y} width={bar.w} height={bar.h} rx={0.5} />
      ))}
    </svg>
  );
}

export function AudioRenderer({ artifact, event, ticket }: ArtifactRendererProps) {
  const source = artifactSource(artifact, event, ticket, "audio/wav");
  const label = event.title || event.caption || "Audio artifact";
  const releaseRef = useReleaseMediaOnUnmount<HTMLAudioElement>();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [speed, setSpeed] = useState(1);
  const speedRef = useRef(1);
  const [showTranscript, setShowTranscript] = useState(false);
  useEffect(() => {
    speedRef.current = speed;
    if (audioRef.current) audioRef.current.playbackRate = speed;
  }, [speed]);
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    try {
      audio.load();
    } catch {
      /* jsdom / detached element */
    }
    audio.playbackRate = speedRef.current;
  }, [source]);
  const attachAudioRef = useCallback(
    (node: HTMLAudioElement | null) => {
      audioRef.current = node;
      releaseRef(node);
    },
    [releaseRef],
  );
  const durationSeconds = artifact.duration_ms
    ? artifact.duration_ms / 1000
    : undefined;
  return (
    <div className="artifact-audio-wrap">
      {artifact.peaks && artifact.peaks.length > 0 ? (
        <AudioWaveform peaks={artifact.peaks} label={label} />
      ) : null}
      <MediaControls
        className="artifact-audio-player"
        controlsClassName="artifact-audio-controls"
        initialDuration={durationSeconds}
        mediaKey={source}
        mediaRef={audioRef}
        onSpeedChange={setSpeed}
        showFullscreen={false}
        speed={speed}
        additionalControls={artifact.transcript ? (
          <button
            aria-expanded={showTranscript}
            className="artifact-audio-transcript-toggle"
            onClick={() => setShowTranscript((value) => !value)}
            type="button"
          >
            {showTranscript ? "Hide transcript" : "Show transcript"}
          </button>
        ) : undefined}
      >
        <audio
          ref={attachAudioRef}
          className="artifact-audio"
          preload="metadata"
          aria-label={label}
          src={source}
        />
      </MediaControls>
      {showTranscript && artifact.transcript ? (
        <div
          aria-label="Transcript"
          className="artifact-audio-transcript"
          role="region"
        >
          {artifact.transcript}
        </div>
      ) : null}
    </div>
  );
}
