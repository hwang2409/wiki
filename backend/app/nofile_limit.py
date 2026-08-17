from __future__ import annotations

import logging
import resource
import sys


logger = logging.getLogger(__name__)
DARWIN_FALLBACK_LIMIT = 10240


def raise_nofile_limit() -> tuple[int, int]:
    """Raise the soft file-descriptor limit without blocking startup."""

    try:
        before_soft, before_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError) as exc:
        logger.warning("rlimit_nofile_read_failed error=%s", exc)
        return -1, -1

    after_soft, after_hard = before_soft, before_hard
    if before_soft < before_hard:
        try:
            resource.setrlimit(
                resource.RLIMIT_NOFILE, (before_hard, before_hard)
            )
            after_soft, after_hard = before_hard, before_hard
        except (OSError, ValueError) as exc:
            if sys.platform == "darwin":
                fallback_soft = min(before_hard, DARWIN_FALLBACK_LIMIT)
                try:
                    resource.setrlimit(
                        resource.RLIMIT_NOFILE, (fallback_soft, before_hard)
                    )
                    after_soft, after_hard = fallback_soft, before_hard
                except (OSError, ValueError) as fallback_exc:
                    logger.warning(
                        "rlimit_nofile_raise_failed before_soft=%d "
                        "before_hard=%d error=%s",
                        before_soft,
                        before_hard,
                        fallback_exc,
                    )
            else:
                logger.warning(
                    "rlimit_nofile_raise_failed before_soft=%d "
                    "before_hard=%d error=%s",
                    before_soft,
                    before_hard,
                    exc,
                )

    logger.info(
        "rlimit_nofile before_soft=%d before_hard=%d after_soft=%d "
        "after_hard=%d",
        before_soft,
        before_hard,
        after_soft,
        after_hard,
    )
    return after_soft, after_hard
