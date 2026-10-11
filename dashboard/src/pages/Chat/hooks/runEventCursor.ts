type Cursor = { runId: string; afterSeq: number; retired: Set<string> };
const cursors = new WeakMap<object, Cursor>();

export function acceptRunEvent(session: object, frame: object): boolean {
  const data = frame as Record<string, unknown>;
  if (typeof data.run_id !== "string" || data.seq === undefined) return true;
  if (!Number.isSafeInteger(data.seq) || (data.seq as number) <= 0)
    return false;
  const previous = cursors.get(session);
  if (previous?.retired.has(data.run_id)) return false;
  if (previous?.runId === data.run_id) {
    if ((data.seq as number) <= previous.afterSeq) return false;
    previous.afterSeq = data.seq as number;
  } else {
    const retired = previous?.retired ?? new Set<string>();
    if (previous) retired.add(previous.runId);
    cursors.set(session, {
      runId: data.run_id,
      afterSeq: data.seq as number,
      retired,
    });
  }
  return true;
}

export function runSubscribeCursor(session: object): {
  run_id?: string;
  after_seq?: number;
} {
  const cursor = cursors.get(session);
  return cursor ? { run_id: cursor.runId, after_seq: cursor.afterSeq } : {};
}
