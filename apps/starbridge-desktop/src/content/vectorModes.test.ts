import { describe, expect, it } from "vitest";

import { VECTOR_MODES } from "./vectorModes";

describe("public vector modes", () => {
  it("exposes exactly the four supported public modes", () => {
    expect(VECTOR_MODES.map((mode) => mode.id)).toEqual([
      "smart",
      "artisan",
      "lightweight",
      "exact",
    ]);
    expect(new Set(VECTOR_MODES.map((mode) => mode.id)).size).toBe(4);
  });
});
