import { useEffect, useMemo, useState } from "react";
import {
  CircleAlert,
  CircleCheck,
  LoaderCircle,
  ExternalLink,
  GitBranch,
  GitPullRequest,
  ListChecks,
  MessageSquareQuote,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { approveAgentPr, getAgentPr } from "./api";
import type { AgentPrCheck, AgentPrData } from "./api";
import { externalLinkProps } from "./external-links";
import { SplitDiffView } from "./split-diff";

function humanizeEnum(value: string | null | undefined, fallback = "unknown"): string {
  if (!value) return fallback;
  return value.toLowerCase().replace(/_/g, " ");
}

function shortWhen(value: string | null): string | null {
  if (!value) return null;
  return value.slice(0, 16).replace("T", " ");
}

function checkSummary(checks: AgentPrCheck[]) {
  return checks.reduce(
    (acc, check) => {
      acc[check.state] += 1;
      return acc;
    },
    { pass: 0, fail: 0, pending: 0 }
  );
}

function CheckStateGlyph({ state }: { state: AgentPrCheck["state"] }) {
  if (state === "pass") {
    return <CircleCheck size={12} />;
  }
  if (state === "fail") {
    return <CircleAlert size={12} />;
  }
  return <LoaderCircle size={12} />;
}

export function AgentPrReviewPanel({
  ticket,
  tick,
  canApprove = true,
}: {
  ticket: string;
  tick: number;
  canApprove?: boolean;
}) {
  const [data, setData] = useState<AgentPrData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);

  useEffect(() => {
    setData(null);
    setError(null);
    setLoading(true);
    setConfirming(false);
    setSubmitting(false);
  }, [ticket]);

  useEffect(() => {
    let ignore = false;
    getAgentPr(ticket)
      .then((result) => {
        if (ignore) return;
        setData(result);
        setError(null);
      })
      .catch((err) => {
        if (ignore) return;
        setError(err instanceof Error ? err.message : "Could not load pull request");
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [ticket, tick, refreshNonce]);

  const checks = data?.statusChecks ?? [];
  const unresolvedThreads = data?.unresolvedThreads ?? [];
  const checkCounts = useMemo(() => checkSummary(checks), [checks]);
  const alreadyApproved = data?.reviewDecision === "APPROVED";
  const approveDisabled = !canApprove || !data || data.state !== "OPEN" || alreadyApproved;

  async function approve() {
    if (approveDisabled || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await approveAgentPr(ticket);
      setConfirming(false);
      setRefreshNonce((current) => current + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approve failed");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading && !data) return <div className="pr-review-empty">Loading PR…</div>;
  if (!data) return <div className="pr-review-empty">{error ?? "No PR found for this agent."}</div>;

  return (
    <div className="pr-review">
      <header className="pr-review-header">
        <div className="pr-review-kicker">
          <GitPullRequest size={14} />
          <span>pull request</span>
        </div>
        <div className="pr-review-actions">
          <a
            className="pr-review-link"
            href={data.url}
            {...externalLinkProps(data.url)}
          >
            <ExternalLink size={12} />
            github
          </a>
          <button
            className="pr-review-action"
            type="button"
            onClick={() => setRefreshNonce((current) => current + 1)}
          >
            <RefreshCw size={12} />
            refresh
          </button>
          {alreadyApproved ? (
            <span className="pr-review-approved">
              <ShieldCheck size={12} />
              approved
            </span>
          ) : confirming ? (
            <>
              <span className="pr-review-pill">{data.repo}</span>
              <button
                className="pr-review-action is-strong"
                disabled={submitting}
                type="button"
                onClick={() => void approve()}
              >
                <ShieldCheck size={12} />
                {submitting ? "approving…" : `confirm approve ${data.repo}`}
              </button>
              <button
                className="pr-review-action"
                disabled={submitting}
                type="button"
                onClick={() => setConfirming(false)}
              >
                cancel
              </button>
            </>
          ) : canApprove ? (
            <button
              className="pr-review-action is-strong"
              disabled={approveDisabled}
              type="button"
              onClick={() => setConfirming(true)}
            >
              <ShieldCheck size={12} />
              approve
            </button>
          ) : null}
        </div>
        <h2 className="pr-review-title">{data.title}</h2>
        <div className="pr-review-meta">
          <span className="pr-review-pill">{humanizeEnum(data.state)}</span>
          <span className="pr-review-pill">{humanizeEnum(data.reviewDecision, "review undecided")}</span>
          <span className="pr-review-pill">{humanizeEnum(data.mergeable)}</span>
          <span className="pr-review-pill">{humanizeEnum(data.mergeStateStatus)}</span>
          {data.headRefName ? (
            <span className="pr-review-pill">
              <GitBranch size={11} />
              {data.headRefName}
            </span>
          ) : null}
        </div>
        <div className="pr-review-stats">
          <span>{data.changedFiles} files</span>
          <span>+{data.additions}</span>
          <span>-{data.deletions}</span>
          <span>{checkCounts.fail} fail</span>
          <span>{checkCounts.pending} pending</span>
          <span>{checkCounts.pass} pass</span>
          <span>{unresolvedThreads.length} threads</span>
        </div>
      </header>

      {error ? <div className="pr-review-banner">{error}</div> : null}

      <section className="pr-review-section">
        <div className="pr-review-section-head">
          <ListChecks size={13} />
          <span>checks</span>
        </div>
        {checks.length === 0 ? (
          <div className="pr-review-empty is-inline">No checks reported.</div>
        ) : (
          <div className="pr-review-checks">
            {checks.map((check, index) => {
              const context = check.workflow ?? "status check";
              const content = (
                <>
                  <span className={`pr-review-check-status is-${check.state}`} aria-label={check.state}>
                    <CheckStateGlyph state={check.state} />
                  </span>
                  <span className="pr-review-check-copy">
                    <span className="pr-review-check-name">{check.name}</span>
                    <span className="pr-review-check-context">{context}</span>
                  </span>
                  <span className="pr-review-check-result">{humanizeEnum(check.rawState)}</span>
                </>
              );
              return check.detailsUrl ? (
                <a
                  className="pr-review-check"
                  href={check.detailsUrl}
                  key={`${check.name}-${index}`}
                  {...externalLinkProps(check.detailsUrl)}
                >
                  {content}
                </a>
              ) : (
                <div className="pr-review-check" key={`${check.name}-${index}`}>
                  {content}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="pr-review-section">
        <div className="pr-review-section-head">
          <MessageSquareQuote size={13} />
          <span>unresolved threads</span>
        </div>
        {unresolvedThreads.length === 0 ? (
          <div className="pr-review-empty is-inline">No unresolved review threads.</div>
        ) : (
          <div className="pr-review-threads">
            {unresolvedThreads.map((thread, index) => (
              <article className="pr-review-thread" key={`${thread.path}-${index}`}>
                <div className="pr-review-thread-meta">
                  <span className="pr-review-thread-path">{thread.path}</span>
                  {thread.author ? (
                    <span className="pr-review-thread-detail">{thread.author}</span>
                  ) : null}
                  {thread.updatedAt ? (
                    <span className="pr-review-thread-detail">{shortWhen(thread.updatedAt)}</span>
                  ) : null}
                  {thread.url ? (
                    <a
                      className="pr-review-thread-link"
                      href={thread.url}
                      {...externalLinkProps(thread.url)}
                    >
                      <ExternalLink size={11} />
                      comment
                    </a>
                  ) : null}
                </div>
                <div className="pr-review-thread-comment">{thread.latestComment || "(no comment text)"}</div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="pr-review-section">
        <div className="pr-review-section-head">
          <GitPullRequest size={13} />
          <span>diff</span>
        </div>
        <SplitDiffView
          className="pr-review-diff"
          emptyClassName="pr-review-empty is-inline"
          emptyMessage="No diff available."
          patch={data.diff}
        />
      </section>
    </div>
  );
}
