import { expect, test } from "vitest";
import type { TraceCall, TraceDetail } from "./api";
import {
  formatBytes,
  formatDuration,
  judgeVersionCount,
  seedVersions,
  statusTone,
  toTimeline,
} from "./trace";

const call = (over: Partial<TraceCall> & Pick<TraceCall, "id" | "role" | "upstream_model_id">): TraceCall => ({
  model_id: 1,
  provider_name: "P",
  request_payload: { model: over.upstream_model_id, messages: [] },
  response_content: "out",
  status: "success",
  error_message: "",
  duration_ms: 300,
  round: null,
  prompt_tokens: 3,
  completion_tokens: 2,
  created_at: "2026-09-02T12:00:01+00:00",
  ...over,
});

const detail: TraceDetail = {
  request: {
    id: 1,
    pipeline_name: "council-v1",
    client_model_field: "council-v1",
    request_messages: [{ role: "user", content: "hi" }],
    request_params: {},
    response_content: "final",
    status: "success",
    total_duration_ms: 1200,
    total_prompt_tokens: 10,
    total_completion_tokens: 5,
    created_at: "2026-09-02T12:00:00+00:00",
  },
  calls: [
    call({ id: 1, role: "member", upstream_model_id: "m-a" }),
    call({ id: 2, role: "member", upstream_model_id: "m-b" }),
    call({ id: 3, role: "judge_critique", upstream_model_id: "m-a", response_content: "critique" }),
    call({ id: 4, role: "judge", upstream_model_id: "m-a", response_content: "final" }),
  ],
};

test("toTimeline：请求 → 裁判 → 成员调用 → 最终响应（UT-27-1）", () => {
  const timeline = toTimeline(detail);
  expect(timeline).toHaveLength(5);
  expect(timeline[0].kind).toBe("request");
  expect(timeline[4].kind).toBe("final");
  const members = timeline.filter((entry) => entry.kind === "call");
  expect(members.map((entry) => (entry.kind === "call" ? entry.call.upstream_model_id : ""))).toEqual([
    "m-a",
    "m-b",
  ]);
  // 两段式裁判（评论 + 最终）合入同一裁判组
  const judge = timeline[1];
  expect(judge.kind === "judge" && judge.calls.map((c) => c.role)).toEqual([
    "judge_critique",
    "judge",
  ]);
});

test("toTimeline：多次换裁判重跑合成一组并列保留，置于原始请求之后（UT-30-1）", () => {
  const rerunDetail: TraceDetail = {
    ...detail,
    calls: [
      ...detail.calls,
      call({ id: 5, role: "judge_critique", upstream_model_id: "m-c", status: "failed", error_message: "boom" }),
      call({ id: 6, role: "judge_rerun", upstream_model_id: "m-c", status: "failed", error_message: "boom" }),
      call({ id: 7, role: "judge_critique", upstream_model_id: "m-d" }),
      call({ id: 8, role: "judge_rerun", upstream_model_id: "m-d" }),
    ],
  };
  const timeline = toTimeline(rerunDetail);
  // 请求 + 裁判组 + 成员×2 + 最终响应：重跑版本只并组、不新增卡片
  expect(timeline.map((entry) => entry.kind)).toEqual(["request", "judge", "call", "call", "final"]);
  const judge = timeline[1];
  expect(judge.kind === "judge" && judge.calls.map((c) => c.role)).toEqual([
    "judge_critique",
    "judge",
    "judge_critique",
    "judge_rerun",
    "judge_critique",
    "judge_rerun",
  ]);

  // 透传/无裁判 trace 不产生裁判组
  const passthrough: TraceDetail = {
    ...detail,
    calls: [call({ id: 9, role: "passthrough", upstream_model_id: "m-x" })],
  };
  expect(toTimeline(passthrough).map((entry) => entry.kind)).toEqual(["request", "call", "final"]);
});

test("judgeVersionCount：仅最终段行计数，评论行不计（UT-31-1）", () => {
  expect(judgeVersionCount(detail.calls)).toBe(1);
  const rerunCalls: TraceCall[] = [
    ...detail.calls,
    call({ id: 5, role: "judge_critique", upstream_model_id: "m-c" }),
    call({ id: 6, role: "judge_rerun", upstream_model_id: "m-c" }),
  ];
  expect(judgeVersionCount(rerunCalls)).toBe(2);
  expect(judgeVersionCount([call({ id: 9, role: "judge_critique", upstream_model_id: "m-x" })])).toBe(0);
});

test("格式化：耗时与字节数（UT-27-1）", () => {
  expect(formatDuration(350)).toBe("350ms");
  expect(formatDuration(1200)).toBe("1.2s");
  expect(formatBytes(512)).toBe("512 B");
  expect(formatBytes(2048)).toBe("2.0 KB");
  expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
});

test("状态配色映射（UT-27-1）", () => {
  expect(statusTone("success")).toBe("success");
  expect(statusTone("degraded")).toBe("warn");
  expect(statusTone("failed")).toBe("danger");
  expect(statusTone("client_cancelled")).toBe("muted");
});

/** ICE 多轮 trace：member(r0)×2 → critique(r0) → member(r1)×2 → critique(r1) → judge(null)。 */
const iceDetail: TraceDetail = {
  ...detail,
  request: { ...detail.request, pipeline_name: "ice-v1", client_model_field: "ice-v1" },
  calls: [
    call({ id: 1, role: "member", upstream_model_id: "m-a", round: 0 }),
    call({ id: 2, role: "member", upstream_model_id: "m-b", round: 0 }),
    call({ id: 3, role: "judge_critique", upstream_model_id: "m-a", round: 0, response_content: "c0" }),
    call({ id: 4, role: "member", upstream_model_id: "m-a", round: 1 }),
    call({ id: 5, role: "member", upstream_model_id: "m-b", round: 1 }),
    call({ id: 6, role: "judge_critique", upstream_model_id: "m-a", round: 1, response_content: "c1" }),
    call({ id: 7, role: "judge", upstream_model_id: "m-a", response_content: "final" }),
  ],
};

test("toTimeline：ICE 成员行按 round 分组且组内保序，评论行归裁判组按 round 排序（UT-36-1）", () => {
  const timeline = toTimeline(iceDetail);
  expect(timeline.map((entry) => entry.kind)).toEqual([
    "request",
    "judge",
    "member_group",
    "member_group",
    "final",
  ]);
  const groups = timeline.filter((entry) => entry.kind === "member_group");
  expect(groups.map((entry) => (entry.kind === "member_group" ? entry.round : -1))).toEqual([0, 1]);
  // 组内保落库顺序
  expect(
    groups.map((entry) =>
      entry.kind === "member_group" ? entry.calls.map((c) => c.upstream_model_id) : [],
    ),
  ).toEqual([
    ["m-a", "m-b"],
    ["m-a", "m-b"],
  ]);
  // critique 行按 round 排序进裁判组，终局（round=null）排最后
  const judge = timeline[1];
  expect(judge.kind === "judge" && judge.calls.map((c) => [c.role, c.round])).toEqual([
    ["judge_critique", 0],
    ["judge_critique", 1],
    ["judge", null],
  ]);

  // null round 的成员归单组（与有轮次的组并存）
  const mixed: TraceDetail = {
    ...iceDetail,
    calls: [
      call({ id: 1, role: "member", upstream_model_id: "m-a", round: 0 }),
      call({ id: 2, role: "member", upstream_model_id: "m-x" }),
      call({ id: 3, role: "member", upstream_model_id: "m-y" }),
      call({ id: 4, role: "judge", upstream_model_id: "m-a" }),
    ],
  };
  const mixedGroups = toTimeline(mixed).filter((entry) => entry.kind === "member_group");
  expect(
    mixedGroups.map((entry) => (entry.kind === "member_group" ? entry.round : -1)),
  ).toEqual([0, null]);
  const nullGroup = mixedGroups[1];
  expect(
    nullGroup.kind === "member_group" && nullGroup.calls.map((c) => c.upstream_model_id),
  ).toEqual(["m-x", "m-y"]);

  // 全 null（council/透传）保持逐行 call 条目，无 member_group（零变化路径）
  expect(toTimeline(detail).some((entry) => entry.kind === "member_group")).toBe(false);
});

test("seedVersions：ICE 的 N 条轮评论全归原始裁判版本，重跑各配对自己的 1 条；版本数不膨胀（UT-36-2）", () => {
  const judgeCalls = iceDetail.calls.filter((c) => c.role.startsWith("judge"));
  const versions = seedVersions(judgeCalls);
  expect(versions.size).toBe(1);
  const original = versions.get(1);
  expect(original?.critiques.map((c) => [c.round, c.content])).toEqual([
    [0, "c0"],
    [1, "c1"],
  ]);
  expect(judgeVersionCount(judgeCalls)).toBe(1);

  // 换裁判重跑（模型 2）：critique + judge_rerun 成对追加，各自配对自己的 1 条评论
  const withRerun: TraceCall[] = [
    ...judgeCalls,
    call({ id: 8, role: "judge_critique", model_id: 2, upstream_model_id: "m-b", response_content: "rc" }),
    call({ id: 9, role: "judge_rerun", model_id: 2, upstream_model_id: "m-b", response_content: "rf" }),
  ];
  const rerunVersions = seedVersions(withRerun);
  expect(rerunVersions.size).toBe(2);
  expect(rerunVersions.get(1)?.critiques).toHaveLength(2);
  expect(rerunVersions.get(2)?.critiques.map((c) => c.content)).toEqual(["rc"]);
  expect(judgeVersionCount(withRerun)).toBe(2);

  // 失败重跑仍消耗其评论行，但不占版本
  const withFailedRerun: TraceCall[] = [
    ...withRerun,
    call({ id: 10, role: "judge_critique", model_id: 2, upstream_model_id: "m-b", status: "failed" }),
    call({ id: 11, role: "judge_rerun", model_id: 2, upstream_model_id: "m-b", status: "failed" }),
    call({ id: 12, role: "judge_critique", model_id: 2, upstream_model_id: "m-b", response_content: "rc2" }),
    call({ id: 13, role: "judge_rerun", model_id: 2, upstream_model_id: "m-b", response_content: "rf2" }),
  ];
  const finalVersions = seedVersions(withFailedRerun);
  expect(finalVersions.size).toBe(2);
  expect(finalVersions.get(2)?.content).toBe("rf2");
  expect(finalVersions.get(2)?.critiques.map((c) => c.content)).toEqual(["rc2"]);
  // judgeVersionCount 只数最终段行（含失败重跑行），评论行不膨胀计数
  expect(judgeVersionCount(withFailedRerun)).toBe(4);
});
