export const DEFAULT_BASE_URL = 'http://localhost:7779';

const STORAGE_KEY = 'mantis_base_url';

export function getBaseUrl(): string {
  return localStorage.getItem(STORAGE_KEY) ?? DEFAULT_BASE_URL;
}

export function setBaseUrl(url: string): void {
  localStorage.setItem(STORAGE_KEY, url.replace(/\/+$/, ''));
}

export function getApiToken(): string {
  return localStorage.getItem(`mantis_api_token:${getBaseUrl()}`) ?? '';
}

export function setApiToken(token: string): void {
  const key = `mantis_api_token:${getBaseUrl()}`;
  if (token.trim()) localStorage.setItem(key, token.trim());
  else localStorage.removeItem(key);
}
