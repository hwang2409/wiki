const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;
const MONTH = 30 * DAY;
const YEAR = 365 * DAY;

export function formatRelative(iso: string, nowMs: number): string {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const signed = nowMs - then;
  const past = signed >= 0;
  const delta = Math.abs(signed);
  if (delta < 45 * SECOND) return past ? "just now" : "in a moment";
  if (delta < MINUTE + 30 * SECOND) return past ? "1m ago" : "in 1m";
  if (delta < HOUR) {
    const m = Math.round(delta / MINUTE);
    return past ? `${m}m ago` : `in ${m}m`;
  }
  if (delta < 90 * MINUTE) return past ? "1h ago" : "in 1h";
  if (delta < DAY) {
    const h = Math.round(delta / HOUR);
    return past ? `${h}h ago` : `in ${h}h`;
  }
  if (delta < 2 * DAY) return past ? "yesterday" : "tomorrow";
  if (delta < WEEK) {
    const d = Math.round(delta / DAY);
    return past ? `${d}d ago` : `in ${d}d`;
  }
  if (delta < MONTH) {
    const w = Math.round(delta / WEEK);
    const label = w === 1 ? "week" : "weeks";
    return past ? `${w} ${label} ago` : `in ${w} ${label}`;
  }
  if (delta < YEAR) {
    const mo = Math.round(delta / MONTH);
    return past ? `${mo}mo ago` : `in ${mo}mo`;
  }
  const y = Math.round(delta / YEAR);
  return past ? `${y}y ago` : `in ${y}y`;
}

const absoluteFormatter = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  timeZoneName: "short",
});

export function formatAbsolute(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString()}\n${absoluteFormatter.format(d)}`;
}
