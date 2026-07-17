/**
 * P2-R1 — list_pages returns source_id and honors request source_id filter.
 */
import { describe, test, expect, beforeAll, afterAll } from "bun:test";
import { PGLiteEngine } from "../../src/core/pglite-engine.ts";
import { operations } from "../../src/core/operations.ts";
import type { OperationContext } from "../../src/core/operations.ts";

const listPagesOp = operations.find((op) => op.name === "list_pages")!;

let engine: PGLiteEngine;

beforeAll(async () => {
  engine = new PGLiteEngine();
  await engine.connect({ engine: "pglite" } as never);
  await engine.initSchema();
  // Ensure two sources exist if needed — putPage uses default unless configured.
  // Seed pages in default source with distinct slugs first.
  await engine.putPage("p2/alpha", { type: "note", title: "Alpha", compiled_truth: "a" });
  await engine.putPage("p2/beta", { type: "note", title: "Beta", compiled_truth: "b" });
});

afterAll(async () => {
  if (engine) await engine.disconnect();
});

function makeCtx(overrides: Partial<OperationContext> = {}): OperationContext {
  return {
    engine,
    config: {} as never,
    remote: true,
    sourceId: "default",
    auth: { allowedSources: ["default", "raclaw-canonical"] },
    ...overrides,
  } as OperationContext;
}

describe("P2-R1 list_pages source_id", () => {
  test("response rows include source_id", async () => {
    const rows = (await listPagesOp.handler(makeCtx(), { limit: 50 })) as Array<Record<string, unknown>>;
    expect(Array.isArray(rows)).toBe(true);
    expect(rows.length).toBeGreaterThan(0);
    for (const r of rows) {
      expect(typeof r.source_id).toBe("string");
      expect(r.source_id).toBeTruthy();
      expect(r.slug).toBeTruthy();
    }
  });

  test("source_id filter within grant narrows results", async () => {
    const all = (await listPagesOp.handler(makeCtx(), { limit: 50 })) as Array<Record<string, unknown>>;
    const filtered = (await listPagesOp.handler(makeCtx(), {
      limit: 50,
      source_id: "default",
    })) as Array<Record<string, unknown>>;
    expect(filtered.length).toBeGreaterThan(0);
    for (const r of filtered) {
      expect(r.source_id).toBe("default");
    }
    // filtered should be subset of grant-visible
    expect(filtered.length).toBeLessThanOrEqual(all.length);
  });

  test("source_id outside grant is denied for remote", async () => {
    await expect(
      listPagesOp.handler(makeCtx(), { limit: 10, source_id: "raclaw-memory" }),
    ).rejects.toThrow(/permission_denied|outside your granted sources/i);
  });
});
