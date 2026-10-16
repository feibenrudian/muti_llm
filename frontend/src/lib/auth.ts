/** 管理台会话令牌存取（多用户 T46）：sessionStorage，标签页生命周期。 */

const STORAGE_KEY = "muti.session";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  sessionStorage.setItem(STORAGE_KEY, token);
}

export function clearToken(): void {
  sessionStorage.removeItem(STORAGE_KEY);
}
