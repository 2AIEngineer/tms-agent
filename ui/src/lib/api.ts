import type {
  AlertDetail,
  AlertRow,
  AlertStatus,
  AlertSummary,
  AssistantAnswer,
  ConversationMessage,
  ConversationSummary,
  Me,
  MissionsResponse,
  MonitoringResponse,
  PendingAction,
  ReportItem,
  Severity,
} from './types'

const STORAGE_KEY = 'tms-assistant.settings'

export interface Settings {
  /** Base de l'API. `/api` passe par le proxy Vite en développement. */
  baseUrl: string
  /** Jeton porteur Entra ID. Vide en développement (OPS_API_AUTH_DISABLED). */
  token: string
}

const DEFAULTS: Settings = { baseUrl: import.meta.env.VITE_API_URL ?? '/api', token: '' }

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : DEFAULTS
  } catch {
    return DEFAULTS
  }
}

export function saveSettings(settings: Settings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings))
  } catch {
    /* stockage indisponible : la session courante garde les réglages en mémoire */
  }
  current = settings
}

let current: Settings = loadSettings()
export const getSettings = () => current

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** Message lisible pour une réponse en erreur : le `detail` de FastAPI quand il existe. */
async function failure(response: Response): Promise<ApiError> {
  let message = `Erreur ${response.status}`
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string') message = body.detail
    else if (Array.isArray(body?.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join(' · ')
  } catch {
    /* corps non JSON */
  }
  return new ApiError(response.status, message)
}

export function headers(json = false): HeadersInit {
  const out: Record<string, string> = {}
  if (current.token) out.Authorization = `Bearer ${current.token}`
  if (json) out['Content-Type'] = 'application/json'
  return out
}

export const url = (path: string) => `${current.baseUrl.replace(/\/$/, '')}${path}`

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(url(path), { ...init, headers: { ...headers(!!init.body), ...init.headers } })
  } catch {
    throw new ApiError(0, "API injoignable. Vérifiez l'adresse dans les réglages.")
  }
  if (!response.ok) throw await failure(response)
  return response.json() as Promise<T>
}

const query = (params: Record<string, string | number | undefined | null>) => {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') q.set(k, String(v))
  const s = q.toString()
  return s ? `?${s}` : ''
}

export const api = {
  health: () => request<{ ok: boolean; database: string }>('/health'),
  me: () => request<Me>('/assistant/me'),
  monitoring: () => request<MonitoringResponse>('/monitoring'),

  conversations: () =>
    request<{ count: number; conversations: ConversationSummary[] }>('/assistant/conversations'),
  conversation: (id: string) =>
    request<{ conversation_id: string; count: number; messages: ConversationMessage[] }>(
      `/assistant/conversations/${id}`,
    ),
  pending: () => request<{ count: number; pending_actions: PendingAction[] }>('/assistant/pending'),
  reports: () => request<{ count: number; reports: ReportItem[] }>('/assistant/reports?limit=20'),
  decide: (id: string, decision: 'confirm' | 'reject') =>
    request<AssistantAnswer>(`/assistant/actions/${id}/${decision}`, { method: 'POST' }),

  alerts: (p: { status?: AlertStatus; min_severity?: Severity; mission_id?: string; limit?: number; offset?: number }) =>
    request<{ count: number; total: number; alerts: AlertRow[] }>(`/alerts${query(p)}`),
  alertSummary: () => request<AlertSummary>('/alerts/summary'),
  alert: (id: string) => request<AlertDetail>(`/alerts/${id}`),

  missions: () => request<MissionsResponse>('/missions'),
}
