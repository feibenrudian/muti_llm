import { expect, test } from "vitest";
import { buildQuery, detectProtocol, parseErrorMessage } from "./api";

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

test("detectProtocol 按 base_url 自动识别协议（UT-20-2）", () => {
  expect(detectProtocol("https://api.anthropic.com")).toBe("anthropic");
  expect(detectProtocol("https://api.anthropic.com/")).toBe("anthropic");
  expect(detectProtocol("api.anthropic.com")).toBe("anthropic"); // 缺 scheme 也能识别
  expect(detectProtocol("https://anthropic-proxy.example.com/v1")).toBe("anthropic");
  expect(detectProtocol("https://api.deepseek.com")).toBe("openai_compatible");
  expect(detectProtocol("https://open.bigmodel.cn/api/coding/paas/v4")).toBe("openai_compatible");
  expect(detectProtocol("http://127.0.0.1:11434/v1")).toBe("openai_compatible");
  expect(detectProtocol("")).toBe("openai_compatible"); // 空值兜底
  expect(detectProtocol("not a url")).toBe("openai_compatible"); // 非法输入兜底
});
