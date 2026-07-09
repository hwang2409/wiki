export function deletePaneStateEntries<T>(cache: Map<string, T>, paneStateKey: string): string[] {
  const prefix = `${paneStateKey}:`;
  const deleted: string[] = [];
  for (const key of [...cache.keys()]) {
    if (!key.startsWith(prefix)) continue;
    cache.delete(key);
    deleted.push(key);
  }
  return deleted;
}

export function createStateKeyWriteBarrier() {
  const blocked = new Set<string>();
  return {
    allows(key: string) {
      return !blocked.has(key);
    },
    block(keys: Iterable<string>) {
      for (const key of keys) blocked.add(key);
    },
    consume(key: string) {
      return blocked.delete(key);
    },
  };
}
