type LoadingPlaceholderProps = {
  className?: string;
  lines?: number[];
};

export function LoadingPlaceholder({
  className = "",
  lines = [100, 86, 92],
}: LoadingPlaceholderProps) {
  return (
    <div
      aria-hidden="true"
      className={`loading-placeholder${className ? ` ${className}` : ""}`}
    >
      {lines.map((width, index) => (
        <span
          className="loading-line"
          key={`${width}-${index}`}
          style={{ width: `${width}%` }}
        />
      ))}
    </div>
  );
}
