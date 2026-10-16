import { describe, expect, it } from "vitest";

/** 用户管理纯函数 vitest（UT-47-1 / UT-49-4 / UT-51-3）。 */

import {
  isKeyStale,
  selectionEqualsPayload,
  selectionToPayload,
  validateUsername,
} from "./users";

describe("validateUsername", () => {
  it("UT-47-1 用户名校验：合法通过；过短/非法字符/超长拦截", () => {
    expect(validateUsername("alice")).toBe("");
    expect(validateUsername("a.b-c_d1")).toBe("");
    expect(validateUsername("ab")).toContain("3-32");
    expect(validateUsername("有中文")).toContain("3-32");
    expect(validateUsername("bad space")).toContain("3-32");
    expect(validateUsername("a".repeat(33))).toContain("3-32");
  });
});

describe("grants selection", () => {
  it("UT-49-4 勾选集合 ↔ 提交载荷：互转与比较", () => {
    const sel = {
      pipelines: new Set([3, 1]),
      models: new Set([2]),
      providers: new Set<number>(),
    };
    expect(selectionToPayload(sel)).toEqual({
      pipelines: [1, 3],
      models: [2],
      providers: [],
    });
    expect(selectionEqualsPayload(sel, { pipelines: [1, 3], models: [2], providers: [] })).toBe(
      true,
    );
    expect(selectionEqualsPayload(sel, { pipelines: [1], models: [2], providers: [] })).toBe(false);
  });
});

describe("isKeyStale", () => {
  it("UT-51-3 长期未使用判定：30 天阈值", () => {
    const now = Date.now();
    const day = 24 * 3600 * 1000;
    const iso = (t: number) => new Date(t).toISOString();
    expect(isKeyStale(iso(now - 31 * day), iso(now - 40 * day))).toBe(true);
    expect(isKeyStale(null, iso(now - 31 * day))).toBe(true);
    expect(isKeyStale(iso(now - 1 * day), iso(now - 40 * day))).toBe(false);
    expect(isKeyStale(null, iso(now - 1 * day))).toBe(false);
    expect(isKeyStale("not-a-date", iso(now))).toBe(false);
  });
});

import { userLabel } from "./users";

describe("userLabel", () => {
  it("UT-50-1 历史行文案映射：user_id=0 且无用户名 → （历史）", () => {
    expect(userLabel({ user_id: 0, user_username: "" })).toBe("（历史）");
    expect(userLabel({ user_id: 1, user_username: "alice" })).toBe("alice");
    expect(userLabel({ user_id: 3, user_username: "" })).toBe("（历史）"); // 防御性兜底
  });
});
