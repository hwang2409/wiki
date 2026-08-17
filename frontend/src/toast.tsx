import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { AlertCircle, AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

export type ToastTone = "message" | "success" | "warning" | "error";

export interface ToastOptions {
  description?: ReactNode;
  duration?: number;
}

interface ToastRecord {
  id: number;
  tone: ToastTone;
  title: ReactNode;
  description?: ReactNode;
  duration: number;
}

const DEFAULT_DURATION: Record<ToastTone, number> = {
  message: 4000,
  success: 4000,
  warning: 6000,
  error: 8000,
};

const EXIT_MS = 160;

let nextId = 1;
let queue: ToastRecord[] = [];
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function snapshot() {
  return queue;
}

function remove(id: number) {
  const next = queue.filter((entry) => entry.id !== id);
  if (next.length === queue.length) return;
  queue = next;
  emit();
}

function show(tone: ToastTone, title: ReactNode, options?: ToastOptions): number {
  const id = nextId++;
  queue = [
    ...queue,
    {
      id,
      tone,
      title,
      description: options?.description,
      duration: options?.duration ?? DEFAULT_DURATION[tone],
    },
  ];
  emit();
  return id;
}

export const toast = {
  message: (title: ReactNode, options?: ToastOptions) => show("message", title, options),
  success: (title: ReactNode, options?: ToastOptions) => show("success", title, options),
  warning: (title: ReactNode, options?: ToastOptions) => show("warning", title, options),
  error: (title: ReactNode, options?: ToastOptions) => show("error", title, options),
  dismiss: (id: number) => remove(id),
};

function iconFor(tone: ToastTone) {
  switch (tone) {
    case "success":
      return CheckCircle2;
    case "warning":
      return AlertTriangle;
    case "error":
      return AlertCircle;
    case "message":
      return Info;
  }
}

function ToastCard({ record }: { record: ToastRecord }) {
  const [mounted, setMounted] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const enterFrameRef = useRef<number | null>(null);
  const dismissTimerRef = useRef<number | null>(null);
  const removeTimerRef = useRef<number | null>(null);

  useEffect(() => {
    enterFrameRef.current = window.requestAnimationFrame(() => setMounted(true));
    return () => {
      if (enterFrameRef.current !== null) {
        window.cancelAnimationFrame(enterFrameRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!Number.isFinite(record.duration) || record.duration <= 0) return;
    dismissTimerRef.current = window.setTimeout(() => setLeaving(true), record.duration);
    return () => {
      if (dismissTimerRef.current !== null) window.clearTimeout(dismissTimerRef.current);
    };
  }, [record.duration]);

  useEffect(() => {
    if (!leaving) return;
    removeTimerRef.current = window.setTimeout(() => remove(record.id), EXIT_MS);
    return () => {
      if (removeTimerRef.current !== null) window.clearTimeout(removeTimerRef.current);
    };
  }, [leaving, record.id]);

  const Icon = iconFor(record.tone);
  const isAlert = record.tone === "error" || record.tone === "warning";
  const classes = [
    "bb-toast",
    `is-${record.tone}`,
    mounted && !leaving ? "is-mounted" : "",
    leaving ? "is-leaving" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      aria-live={isAlert ? "assertive" : "polite"}
      className={classes}
      data-testid="bb-toast"
      data-tone={record.tone}
      role={isAlert ? "alert" : "status"}
    >
      <span aria-hidden="true" className="bb-toast__icon">
        <Icon size={16} />
      </span>
      <div className="bb-toast__copy">
        <div className="bb-toast__title">{record.title}</div>
        {record.description ? (
          <div className="bb-toast__description">{record.description}</div>
        ) : null}
      </div>
      <button
        aria-label="Dismiss"
        className="bb-toast__close"
        type="button"
        onClick={() => setLeaving(true)}
      >
        <X size={12} />
      </button>
    </div>
  );
}

export function Toaster() {
  const records = useSyncExternalStore(subscribe, snapshot, snapshot);
  if (records.length === 0) return null;
  return (
    <div className="bb-toaster" data-testid="bb-toaster">
      {records.map((record) => (
        <ToastCard key={record.id} record={record} />
      ))}
    </div>
  );
}
