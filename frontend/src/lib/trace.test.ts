import { expect, test } from "vitest";
import type { TraceCall, TraceDetail } from "./api";
import { formatBytes, formatDuration, statusTone, toTimeline } from "./trace";

const call = (over: Partial<TraceCall> & Pick<TraceCall, "id" | "role" | "upstream_model_id">): TraceCall => ({
  model_id: 1,
  provider_name: "P",
  request_payload: { model: over.upstream_model_id, messages: [] },
  response_content: "out",
  status: "success",
  error_message: "",
  duration_ms: 300,
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
    call({ id: 3, role: "judge", upstream_model_id: "m-a", response_content: "final" }),
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
  const judge = timeline[1];
  expect(judge.kind === "judge" && judge.calls).toHaveLength(1);
  expect(judge.kind === "judge" && judge.calls[0].upstream_model_id).toBe("m-a");
});

test("toTimeline：多次换裁判重跑合成一组并列保留，置于原始请求之后（UT-30-1）", () => {
  const rerunDetail: TraceDetail = {
    ...detail,
    calls: [
      ...detail.calls,
      call({ id: 4, role: "judge_rerun", upstream_model_id: "m-c", status: "failed", error_message: "boom" }),
      call({ id: 5, role: "judge_rerun", upstream_model_id: "m-d" }),
    ],
  };
  const timeline = toTimeline(rerunDetail);
  // 请求 + 裁判组 + 成员×2 + 最终响应：重跑版本只并组、不新增卡片
  expect(timeline.map((entry) => entry.kind)).toEqual(["request", "judge", "call", "call", "final"]);
  const judge = timeline[1];
  expect(judge.kind === "judge" && judge.calls.map((c) => c.role)).toEqual([
    "judge",
    "judge_rerun",
    "judge_rerun",
  ]);

  // 透传/无裁判 trace 不产生裁判组
  const passthrough: TraceDetail = {
    ...detail,
    calls: [call({ id: 9, role: "passthrough", upstream_model_id: "m-x" })],
  };
  expect(toTimeline(passthrough).map((entry) => entry.kind)).toEqual(["request", "call", "final"]);
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
