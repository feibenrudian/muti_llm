import { expect, test } from "vitest";
import { buildQuery, parseErrorMessage } from "./api";

test("buildQuery 序列化并跳过空值（UT-20-1）", () => {
  expect(
    buildQuery({ status: "failed", page: 2, page_size: 20, keyword: "", model: undefined, pipeline: null }),
  ).toBe("?status=failed&page=2&page_size=20");
  expect(buildQuery({})).toBe("");
});

test("parseErrorMessage 兼容 FastAPI 与 OpenAI 错误结构（UT-20-1）", () => {
  expect(parseErrorMessage({ detail: "Provider 下仍有 1 个模型" })).toBe("Provider 下仍有 1 个模型");
  expect(
    parseErrorMessage({ detail: [{ type: "literal_error", msg: "Input should be 'openai_compatible'" }] }),
  ).toBe("Input should be 'openai_compatible'");
  expect(parseErrorMessage({ error: { message: "Invalid API key", type: "invalid_request_error" } })).toBe(
    "Invalid API key",
  );
  expect(parseErrorMessage("裸文本错误")).toBe("裸文本错误");
  expect(parseErrorMessage(null)).toBe("请求失败");
});
