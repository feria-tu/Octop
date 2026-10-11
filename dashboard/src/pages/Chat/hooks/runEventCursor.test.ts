import { describe, expect, it } from "vitest";
import { acceptRunEvent, runSubscribeCursor } from "./runEventCursor";

describe("durable run event cursors", () => {
  it("replays missing events once and resets for the next run", () => {
    const session = {};
    expect(acceptRunEvent(session, { run_id: "a", seq: 1 })).toBe(true);
    expect(acceptRunEvent(session, { run_id: "a", seq: 3 })).toBe(true);
    expect(runSubscribeCursor(session)).toEqual({ run_id: "a", after_seq: 3 });
    expect(acceptRunEvent(session, { run_id: "a", seq: 3 })).toBe(false);
    expect(acceptRunEvent(session, { run_id: "a", seq: 4 })).toBe(true);
    expect(acceptRunEvent(session, { run_id: "b", seq: 1 })).toBe(true);
    expect(runSubscribeCursor(session)).toEqual({ run_id: "b", after_seq: 1 });
    expect(acceptRunEvent(session, { run_id: "a", seq: 5 })).toBe(false);
  });

  it("keeps legacy transport frames and rejects invalid sequence numbers", () => {
    const session = {};
    expect(acceptRunEvent(session, { type: "pong" })).toBe(true);
    expect(acceptRunEvent(session, { run_id: "a", seq: -1 })).toBe(false);
    expect(runSubscribeCursor(session)).toEqual({});
  });
});
