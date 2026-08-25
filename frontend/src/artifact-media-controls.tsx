import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from "react";

export const MEDIA_SPEED_OPTIONS: readonly number[] = [0.75, 1, 1.25, 1.5, 2];

export function formatMediaDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const mm = String(minutes).padStart(hours ? 2 : 1, "0");
  const ss = String(secs).padStart(2, "0");
  return hours ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

type MediaIconName = "play" | "pause" | "volume" | "muted" | "fullscreen" | "exit-fullscreen";

function showMediaDialog(dialog: HTMLDialogElement) {
  if (dialog.showModal) dialog.showModal();
  else dialog.open = true;
}

function closeMediaDialog(dialog: HTMLDialogElement) {
  if (dialog.close) dialog.close();
  else dialog.open = false;
}

function MediaIcon({ name }: { name: MediaIconName }) {
  const common = {
    "aria-hidden": true,
    fill: "none",
    height: 16,
    stroke: "currentColor",
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    strokeWidth: 1.6,
    viewBox: "0 0 16 16",
    width: 16,
  };
  switch (name) {
    case "play":
      return <svg {...common}><path d="m5 3 7 5-7 5z" fill="currentColor" stroke="none" /></svg>;
    case "pause":
      return <svg {...common}><path d="M5 3.5v9M11 3.5v9" /></svg>;
    case "volume":
      return <svg {...common}><path d="M3 6.5h2l3-2.5v8L5 9.5H3z" /><path d="M10 6a3 3 0 0 1 0 4M12 4.5a5 5 0 0 1 0 7" /></svg>;
    case "muted":
      return <svg {...common}><path d="M3 6.5h2l3-2.5v8L5 9.5H3z" /><path d="m11 6 3 4M14 6l-3 4" /></svg>;
    case "fullscreen":
      return <svg {...common}><path d="M6 3H3v3M10 3h3v3M6 13H3v-3M10 13h3v-3" /></svg>;
    case "exit-fullscreen":
      return <svg {...common}><path d="M6 6H3V3M10 6h3V3M6 10H3v3M10 10h3v3" /></svg>;
  }
}

type MediaControlsProps = {
  children: ReactNode;
  mediaRef: RefObject<HTMLMediaElement | null>;
  mediaLabel: string;
  initialDuration?: number;
  mediaKey: string;
  speed: number;
  onSpeedChange: (speed: number) => void;
  showFullscreen: boolean;
  className: string;
  controlsClassName: string;
  additionalControls?: ReactNode;
};

export function MediaControls({
  children,
  mediaRef,
  mediaLabel,
  initialDuration,
  mediaKey,
  speed,
  onSpeedChange,
  showFullscreen,
  className,
  controlsClassName,
  additionalControls,
}: MediaControlsProps) {
  const playerRef = useRef<HTMLDialogElement | null>(null);
  const expandButtonRef = useRef<HTMLElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(initialDuration ?? 0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [isNativeFullscreen, setIsNativeFullscreen] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);
  const lastNonZeroVolumeRef = useRef(1);
  const wasExpandedRef = useRef(false);
  const dialogTransitionRef = useRef(false);
  const closeExpanded = useCallback(() => {
    setIsExpanded(false);
    window.requestAnimationFrame(() => expandButtonRef.current?.focus());
  }, []);
  const handleDialogClose = useCallback(() => {
    if (dialogTransitionRef.current) return;
    closeExpanded();
  }, [closeExpanded]);

  useLayoutEffect(() => {
    const dialog = playerRef.current;
    if (!dialog) return;
    if (isExpanded) {
      wasExpandedRef.current = true;
      if (dialog.open) {
        dialogTransitionRef.current = true;
        closeMediaDialog(dialog);
        showMediaDialog(dialog);
      } else {
        showMediaDialog(dialog);
      }
      return;
    }
    if (wasExpandedRef.current) {
      wasExpandedRef.current = false;
      if (dialog.open) {
        dialogTransitionRef.current = true;
        closeMediaDialog(dialog);
      }
      dialog.open = true;
      return;
    }
    dialog.open = true;
  }, [isExpanded]);

  const syncFromMedia = useCallback(() => {
    const media = mediaRef.current;
    if (!media) return;
    setPlaying(!media.paused && !media.ended);
    setCurrentTime(Number.isFinite(media.currentTime) ? media.currentTime : 0);
    if (Number.isFinite(media.duration) && media.duration > 0) setDuration(media.duration);
    if (media.volume > 0) lastNonZeroVolumeRef.current = media.volume;
    setVolume(media.volume);
    setMuted(media.muted);
  }, [mediaRef]);

  useEffect(() => {
    const media = mediaRef.current;
    if (!media) return;
    setPlaying(false);
    setCurrentTime(0);
    setDuration(initialDuration ?? 0);
    if (media.volume > 0) lastNonZeroVolumeRef.current = media.volume;
    setVolume(media.volume);
    setMuted(media.muted);
    const events = [
      "durationchange",
      "loadedmetadata",
      "pause",
      "play",
      "progress",
      "timeupdate",
      "volumechange",
    ] as const;
    events.forEach((event) => media.addEventListener(event, syncFromMedia));
    syncFromMedia();
    return () => events.forEach((event) => media.removeEventListener(event, syncFromMedia));
  }, [initialDuration, mediaKey, mediaRef, syncFromMedia]);

  useEffect(() => {
    const updateFullscreen = () => setIsNativeFullscreen(document.fullscreenElement === playerRef.current);
    document.addEventListener("fullscreenchange", updateFullscreen);
    updateFullscreen();
    return () => document.removeEventListener("fullscreenchange", updateFullscreen);
  }, []);

  const togglePlayback = useCallback(() => {
    const media = mediaRef.current;
    if (!media) return;
    if (media.paused || media.ended) {
      void media.play().catch(() => setPlaying(false));
    } else {
      media.pause();
    }
    syncFromMedia();
  }, [mediaRef, syncFromMedia]);

  const setSeek = useCallback((nextTime: number) => {
    const media = mediaRef.current;
    if (!media) return;
    media.currentTime = nextTime;
    setCurrentTime(nextTime);
  }, [mediaRef]);

  const toggleMute = useCallback(() => {
    const media = mediaRef.current;
    if (!media) return;
    if (media.muted) {
      if (media.volume === 0) media.volume = lastNonZeroVolumeRef.current || 1;
      media.muted = false;
      setVolume(media.volume);
      setMuted(false);
      return;
    }
    if (media.volume > 0) lastNonZeroVolumeRef.current = media.volume;
    media.muted = true;
    setMuted(true);
  }, [mediaRef]);

  const setMediaVolume = useCallback((nextVolume: number) => {
    const media = mediaRef.current;
    if (!media) return;
    if (nextVolume > 0) lastNonZeroVolumeRef.current = nextVolume;
    media.volume = nextVolume;
    media.muted = nextVolume === 0;
    setVolume(nextVolume);
    setMuted(media.muted);
  }, [mediaRef]);

  const toggleFullscreen = useCallback(async () => {
    const element = playerRef.current;
    if (!element) return;
    if (isExpanded) {
      closeExpanded();
      return;
    }
    if (document.fullscreenElement === element) {
      try {
        await document.exitFullscreen();
      } catch {
        setIsExpanded(true);
      }
      return;
    }
    if (document.fullscreenEnabled && "requestFullscreen" in element) {
      try {
        await element.requestFullscreen();
        return;
      } catch {
        setIsExpanded(true);
        return;
      }
    }
    setIsExpanded(true);
  }, [closeExpanded, isExpanded]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.target !== event.currentTarget && event.target !== mediaRef.current) return;
    switch (event.key.toLowerCase()) {
      case " ":
      case "k":
        event.preventDefault();
        togglePlayback();
        break;
      case "arrowleft":
        event.preventDefault();
        setSeek(Math.max(0, currentTime - 5));
        break;
      case "arrowright":
        event.preventDefault();
        setSeek(Math.min(duration, currentTime + 5));
        break;
      case "m":
        event.preventDefault();
        toggleMute();
        break;
      case "f":
        if (showFullscreen) {
          event.preventDefault();
          toggleFullscreen();
        }
        break;
    }
  };

  const safeDuration = Math.max(duration, 0);
  const safeCurrentTime = Math.min(Math.max(currentTime, 0), safeDuration);
  const isFullscreen = isNativeFullscreen || isExpanded;
  const keyboardShortcuts = ["Space", "K", "ArrowLeft", "ArrowRight", "M"];
  if (showFullscreen) keyboardShortcuts.push("F", "Escape");
  const player = (
    <dialog
      aria-modal={isExpanded ? "true" : undefined}
      ref={playerRef}
      aria-keyshortcuts={keyboardShortcuts.join(" ")}
      aria-label={mediaLabel}
      className={`artifact-media-player ${className}${isExpanded ? " is-media-expanded" : ""}`}
      onClick={(event) => {
        if (event.target === mediaRef.current) {
          playerRef.current?.focus();
          togglePlayback();
        }
      }}
      onCancel={closeExpanded}
      onKeyDown={handleKeyDown}
      role={isExpanded ? "dialog" : "group"}
      onClose={handleDialogClose}
      tabIndex={0}
    >
      {children}
      <div className={`artifact-media-controls ${controlsClassName} tabular-nums`}>
        <button
          aria-label={playing ? "Pause" : "Play"}
          className="artifact-media-control-button"
          onClick={togglePlayback}
          type="button"
        >
          <MediaIcon name={playing ? "pause" : "play"} />
        </button>
        <span aria-label="Elapsed time" className="artifact-media-time">{formatMediaDuration(safeCurrentTime)}</span>
        <input
          aria-label="Seek"
          aria-valuemax={safeDuration}
          aria-valuemin={0}
          aria-valuenow={safeCurrentTime}
          aria-valuetext={`${formatMediaDuration(safeCurrentTime)} of ${formatMediaDuration(safeDuration)}`}
          className="artifact-media-seek"
          max={safeDuration}
          min={0}
          onChange={(event) => setSeek(Number(event.target.value))}
          role="slider"
          step={0.01}
          type="range"
          value={safeCurrentTime}
        />
        <span aria-label="Duration" className="artifact-media-time">{formatMediaDuration(safeDuration)}</span>
        <div className="artifact-media-volume">
          <button
            aria-label={muted ? "Unmute" : "Mute"}
            className="artifact-media-control-button"
            onClick={toggleMute}
            type="button"
          >
            <MediaIcon name={muted ? "muted" : "volume"} />
          </button>
          <input
            aria-label="Volume"
            aria-valuemax={1}
            aria-valuemin={0}
            aria-valuenow={volume}
            aria-valuetext={`${Math.round(volume * 100)}%`}
            className="artifact-media-volume-slider"
            max={1}
            min={0}
            onChange={(event) => setMediaVolume(Number(event.target.value))}
            role="slider"
            step={0.01}
            type="range"
            value={volume}
          />
        </div>
        <label className="artifact-media-speed">
          <span>Speed</span>
          <select
            aria-label="Playback speed"
            onChange={(event) => onSpeedChange(Number(event.target.value))}
            value={speed}
          >
            {MEDIA_SPEED_OPTIONS.map((option) => (
              <option key={option} value={option}>{option}x</option>
            ))}
          </select>
        </label>
        {showFullscreen ? (
          <button
            aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
            className="artifact-media-control-button"
            onClick={toggleFullscreen}
            ref={(element) => { expandButtonRef.current = element; }}
            type="button"
          >
            <MediaIcon name={isFullscreen ? "exit-fullscreen" : "fullscreen"} />
          </button>
        ) : null}
        {additionalControls}
      </div>
    </dialog>
  );

  return player;
}
