import { Fragment, useEffect, useState } from "react";
import { GitCommit, GitPullRequest, Info } from "lucide-react";
import { externalLinkProps } from "./external-links";
import { getGhPreview, type GhPreviewData } from "./api";

const GITHUB_PREVIEW_URL_PATTERN =
  /^https:\/\/(?:www\.)?github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/(?:pull\/\d+|issues\/\d+|commit\/[0-9a-fA-F]{7,40})\/?$/;
const GITHUB_PREVIEW_TEXT_PATTERN =
  /(https:\/\/(?:www\.)?github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/(?:pull\/\d+|issues\/\d+|commit\/[0-9a-fA-F]{7,40})\/?)/g;
const FRONTEND_CACHE_TTL_MS = 60_000;

export type GitHubPreviewSegment =
  | { type: "text"; value: string }
  | { type: "url"; value: string };

type CacheEntry = {
  data?: GhPreviewData;
  etag?: string | null;
  fetchedAt?: number;
  error?: boolean;
  promise?: Promise<GhPreviewData | null>;
};

const ghPreviewCache = new Map<string, CacheEntry>();

function normalizePreviewUrl(url: string) {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

export function isGitHubPreviewUrl(url?: string) {
  return typeof url === "string" && GITHUB_PREVIEW_URL_PATTERN.test(url);
}

export function containsGitHubPreviewUrl(text: string) {
  GITHUB_PREVIEW_TEXT_PATTERN.lastIndex = 0;
  return GITHUB_PREVIEW_TEXT_PATTERN.test(text);
}

export function splitGitHubPreviewSegments(text: string): GitHubPreviewSegment[] {
  GITHUB_PREVIEW_TEXT_PATTERN.lastIndex = 0;
  return text
    .split(GITHUB_PREVIEW_TEXT_PATTERN)
    .filter(Boolean)
    .map((part) => ({
      type: isGitHubPreviewUrl(part) ? "url" : "text",
      value: part,
    }));
}

function humanizeState(value: string | null | undefined) {
  if (!value) return "unknown";
  return value.toLowerCase().replace(/_/g, " ");
}

function shortSha(value: string | null | undefined) {
  return value ? value.slice(0, 7) : "";
}

function shortTimestamp(value: string | null | undefined) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const now = new Date();
  const sameDay = now.toDateString() === date.toDateString();
  return new Intl.DateTimeFormat(undefined, sameDay ? { hour: "numeric", minute: "2-digit" } : {
    month: "short",
    day: "numeric",
  }).format(date);
}

function summarizeChecks(preview: GhPreviewData) {
  const checks = preview.extra.checks;
  if (!checks) return null;
  if (checks.fail > 0) return `${checks.fail} fail`;
  if (checks.pending > 0) return `${checks.pending} pending`;
  if (checks.pass > 0) return `${checks.pass} pass`;
  return null;
}

function secondaryMeta(preview: GhPreviewData) {
  if (preview.kind === "pr") {
    return summarizeChecks(preview) ?? (
      typeof preview.extra.changedFiles === "number" ? `${preview.extra.changedFiles} files` : null
    );
  }
  if (preview.kind === "commit") {
    return shortSha(preview.extra.sha);
  }
  return null;
}

function previewTimestamp(preview: GhPreviewData) {
  return shortTimestamp(preview.kind === "commit" ? preview.extra.date : preview.extra.updatedAt);
}

function previewIcon(kind: GhPreviewData["kind"]) {
  if (kind === "pr") return <GitPullRequest size={12} />;
  if (kind === "commit") return <GitCommit size={12} />;
  return <Info size={12} />;
}

function hasFreshEntry(entry: CacheEntry | undefined) {
  return Boolean(entry?.fetchedAt && Date.now() - entry.fetchedAt < FRONTEND_CACHE_TTL_MS);
}

function loadGhPreview(url: string) {
  const normalized = normalizePreviewUrl(url);
  const existing = ghPreviewCache.get(normalized) ?? {};
  if (existing.promise) return existing.promise;

  const request = getGhPreview(normalized, existing.etag ?? undefined)
    .then((result) => {
      const next = ghPreviewCache.get(normalized) ?? {};
      next.fetchedAt = Date.now();
      next.error = false;
      if (result.status === 200) {
        next.data = result.data;
        next.etag = result.etag;
        ghPreviewCache.set(normalized, next);
        return result.data;
      }
      if (result.etag) next.etag = result.etag;
      ghPreviewCache.set(normalized, next);
      return next.data ?? null;
    })
    .catch((error) => {
      const next = ghPreviewCache.get(normalized) ?? {};
      next.fetchedAt = Date.now();
      next.error = true;
      ghPreviewCache.set(normalized, next);
      throw error;
    })
    .finally(() => {
      const next = ghPreviewCache.get(normalized);
      if (next) delete next.promise;
    });

  existing.promise = request;
  ghPreviewCache.set(normalized, existing);
  return request;
}

function BarePreviewLink({ url, loading = false }: { url: string; loading?: boolean }) {
  return (
    <>
      <a className="external-link" href={url} title={url} {...externalLinkProps(url)}>
        {url}
      </a>
      {loading ? <span className="gh-preview-loading">loading</span> : null}
    </>
  );
}

export function GhPreviewCard({ url }: { url: string }) {
  const normalizedUrl = normalizePreviewUrl(url);
  const cached = ghPreviewCache.get(normalizedUrl);
  const [preview, setPreview] = useState<GhPreviewData | null>(cached?.data ?? null);
  const [loading, setLoading] = useState(!cached?.data);
  const [failed, setFailed] = useState(Boolean(cached?.error && !cached?.data));

  useEffect(() => {
    let ignore = false;
    const entry = ghPreviewCache.get(normalizedUrl);
    if (entry?.data) {
      setPreview(entry.data);
      setLoading(false);
      setFailed(false);
    }
    if (hasFreshEntry(entry)) return;
    setLoading(!entry?.data);
    loadGhPreview(normalizedUrl)
      .then((data) => {
        if (ignore) return;
        if (data) setPreview(data);
        setLoading(false);
        setFailed(false);
      })
      .catch(() => {
        if (ignore) return;
        setLoading(false);
        setFailed(!entry?.data);
      });
    return () => {
      ignore = true;
    };
  }, [normalizedUrl]);

  if (failed || !preview && !loading) {
    return <BarePreviewLink url={normalizedUrl} />;
  }
  if (!preview) {
    return <BarePreviewLink loading url={normalizedUrl} />;
  }

  const state = humanizeState(preview.state);
  const meta = secondaryMeta(preview);
  const timestamp = previewTimestamp(preview);
  const showState = preview.kind !== "commit";

  return (
    <a
      className={`gh-preview-card is-${preview.kind}`}
      href={normalizedUrl}
      title={normalizedUrl}
      {...externalLinkProps(normalizedUrl)}
    >
      <span className="gh-preview-title">{preview.title}</span>
      <span className="gh-preview-meta">
        <span className="gh-preview-badges">
          {showState ? (
            <span className="gh-preview-badge">
              {previewIcon(preview.kind)}
              <span>{state}</span>
            </span>
          ) : null}
          {meta ? (
            <span className={`gh-preview-badge${showState ? " is-muted" : ""}`}>
              <span>{showState ? "•" : previewIcon(preview.kind)}</span>
              <span>{meta}</span>
            </span>
          ) : null}
        </span>
        {timestamp ? <span className="gh-preview-updated">{timestamp}</span> : null}
      </span>
    </a>
  );
}

export function GitHubPreviewText({ text }: { text: string }) {
  const parts = splitGitHubPreviewSegments(text);
  return (
    <>
      {parts.map((part, index) => {
        if (part.type === "url") {
          return <GhPreviewCard key={`${part.value}:${index}`} url={part.value} />;
        }
        return <Fragment key={`${index}:${part.value.slice(0, 12)}`}>{part.value}</Fragment>;
      })}
    </>
  );
}
