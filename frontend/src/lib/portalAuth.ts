/** 门户会话令牌存取（T52）：sessionStorage，与管理台令牌（lib/auth）分 key 互不覆盖。 */

const STORAGE_KEY = "muti.portal.session";

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
