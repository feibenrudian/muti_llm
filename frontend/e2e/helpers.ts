/** E2E 种子数据工具（经管理 API 造数，被测页面用 UI 操作）。 */

import type { APIRequestContext } from "@playwright/test";

export const SRS_BASE = "http://127.0.0.1:9801/v1";

export async function seedProvider(request: APIRequestContext, name = "e2e-provider"): Promise<number> {
  const resp = await request.post("/api/admin/providers", {
    data: { name, protocol: "openai_compatible", base_url: SRS_BASE, api_key: "sk-e2e-12345678" },
  });
  if (resp.status() === 409) {
    const list = await (await request.get("/api/admin/providers")).json();
    return list.find((item: { name: string }) => item.name === name).id;
  }
  return (await resp.json()).id;
}

export async function seedModel(
  request: APIRequestContext,
  providerId: number,
  displayName: string,
  upstream = "deepseek-v4-flash",
): Promise<number> {
  const resp = await request.post("/api/admin/models", {
    data: { provider_id: providerId, display_name: displayName, upstream_model_id: upstream },
  });
  return (await resp.json()).id;
}

export async function seedCouncilPipeline(
  request: APIRequestContext,
  m1: number,
  m2: number,
  name = "council-v1",
): Promise<number> {
  const resp = await request.post("/api/admin/pipelines", {
    data: {
      name,
      strategy: "council",
      judge_model_id: m1,
      members: [
        { model_id: m1, param_overrides: { temperature: 0.7 } },
        { model_id: m2, param_overrides: { temperature: 0.9 } },
      ],
    },
  });
  return (await resp.json()).id;
}

/** SRS 注入（直连 9801，绕过 vite 代理）。 */
export async function injectSrs(request: APIRequestContext, config: object): Promise<void> {
  const resp = await request.post("http://127.0.0.1:9801/_test/config", { data: config });
  if (!resp.ok()) throw new Error(`srs inject failed: ${resp.status()}`);
}

// 成员温度覆盖 0.7/0.9 与 ICE 录制场景一致（上游请求体含 temperature，逐字节匹配才命中快照）
export async function seedIcePipeline(
  request: APIRequestContext,
  m1: number,
  m2: number,
  name: string,
): Promise<number> {
  const resp = await request.post("/api/admin/pipelines", {
    data: {
      name,
      strategy: "ice",
      judge_model_id: m1,
      members: [
        { model_id: m1, param_overrides: { temperature: 0.7 } },
        { model_id: m2, param_overrides: { temperature: 0.9 } },
      ],
    },
  });
  return (await resp.json()).id;
}

/** ICE 两轮共识场景的录制问题（对应快照 ice_refine_consensus_*）。 */
export const ICE_REFINE_QUESTION =
  "《静夜思》「床前明月光」中的「床」指的是什么？请给出你的判断并简要说明理由。";
