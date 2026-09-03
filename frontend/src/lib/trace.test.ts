import { expect, test } from "vitest";
import type { TraceDetail } from "./api";
import { formatBytes, formatDuration, statusTone, toTimeline } from "./trace";

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
    { id: 1, role: "member", model_id: 1, upstream_model_id: "m-a", provider_name: "P", request_payload: { model: "m-a", messages: [] }, response_content: "a", status: "success", error_message: "", duration_ms: 300, prompt_tokens: 3, completion_tokens: 2, created_at: "2026-09-02T12:00:01+00:00" },
    { id: 2, role: "member", model_id: 2, upstream_model_id: "m-b", provider_name: "P", request_payload: { model: "m-b", messages: [] }, response_content: "b", status: "success", error_message: "", duration_ms: 350, prompt_tokens: 3, completion_tokens: 2, created_at: "2026-09-02T12:00:01+00:00" },
    { id: 3, role: "judge", model_id: 1, upstream_model_id: "m-a", provider_name: "P", request_payload: { model: "m-a", messages: [] }, response_content: "final", status: "success", error_message: "", duration_ms: 500, prompt_tokens: 4, completion_tokens: 1, created_at: "2026-09-02T12:00:02+00:00" },
  ],
};

test("toTimeline：请求 → 调用序列 → 最终响应（UT-27-1）", () => {
  const timeline = toTimeline(detail);
  expect(timeline).toHaveLength(5);
  expect(timeline[0].kind).toBe("request");
  expect(timeline[4].kind).toBe("final");
  const calls = timeline.filter((entry) => entry.kind === "call");
  expect(calls.map((entry) => entry.call?.role)).toEqual(["member", "member", "judge"]);
  expect(calls.map((entry) => entry.call?.upstream_model_id)).toEqual(["m-a", "m-b", "m-a"]);
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
