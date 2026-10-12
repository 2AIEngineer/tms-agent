// Miroir des contrats `shared/mapped_dto` tels que l'API d'exploitation les sérialise.

export type AssistantMode = 'ask' | 'chat' | 'report'
export type Severity = 'low' | 'medium' | 'high' | 'critical'
export type AlertStatus = 'open' | 'resolved'
export type ActionKind = 'close_alert' | 'call_driver' | 'log_decision'
export type PendingActionStatus =
  | 'proposed'
  | 'executing'
  | 'executed'
  | 'failed'
  | 'rejected'
  | 'expired'
export type AssistantStatus = 'answered' | 'awaiting_confirmation' | 'unavailable'
export type ToolRole = 'read' | 'action'

export interface PendingAction {
  id: string
  kind: ActionKind
  status: PendingActionStatus
  summary: string
  reason: string
  conversation_id: string
  arguments: Record<string, string>
  alert_id: string | null
  mission_id: string | null
  tms_id: string | null
  expires_at: string | null
  created_at: string | null
}

export interface ExecutedAction {
  kind: ActionKind
  succeeded: boolean
  detail: string
  alert_id: string | null
  mission_id: string | null
}

export interface ConsultedSource {
  tool: string
  summary: string
}

export interface AssistantAnswer {
  conversation_id: string
  answer: string
  mode: AssistantMode
  status: AssistantStatus
  sources: ConsultedSource[]
  actions: ExecutedAction[]
  pending_actions: PendingAction[]
  truncated: boolean
  unavailable: boolean
  rounds: number
  input_tokens: number
  output_tokens: number
  answered_at: string | null
}

export type AssistantEvent =
  | { type: 'started'; conversation_id: string; mode: AssistantMode; allow_actions: boolean; started_at: string }
  | { type: 'round_started'; round: number; max_rounds: number }
  | { type: 'reasoning_delta'; text: string }
  | { type: 'tool_started'; call_id: string; tool: string; role: ToolRole; arguments: Record<string, string> }
  | {
      type: 'tool_completed'
      call_id: string
      tool: string
      role: ToolRole
      ok: boolean
      duration_ms: number
      preview: string
    }
  | { type: 'text_delta'; text: string }
  | { type: 'pending_action'; action: PendingAction }
  | { type: 'done'; answer: AssistantAnswer }
  | { type: 'error'; message: string; conversation_id?: string | null }

export interface ConversationMessage {
  role: 'operator' | 'assistant' | 'tool'
  content: string
  seq: number
  tool_name: string | null
  created_at: string | null
}

export interface ConversationSummary {
  conversation_id: string
  title: string | null
  mode: AssistantMode
  updated_at: string
}

export interface Me {
  operator_id: string
  name: string
  roles: string[]
  tms_ids: string[]
  can_read: boolean
  can_act: boolean
}

export interface AlertRow {
  id: string
  tms_id: string
  mission_id: string
  rule_id: string
  rule_version: number | string
  severity: Severity
  category: string
  title: string
  message: string
  status: AlertStatus
  detected_at: string
  resolved_at: string | null
  resolution_reason: string | null
  observed_data: Record<string, unknown> | null
}

export interface AlertDetail extends AlertRow {
  investigation: {
    id: string
    status: string
    result: Record<string, unknown> | string | null
    error: string | null
    model: string | null
    input_tokens: number | null
    output_tokens: number | null
    duration_ms: number | null
    completed_at: string | null
  } | null
  notifications: {
    channel: string
    recipient_name: string | null
    recipient_role: string | null
    management_level: string | null
    status: string
    failure_reason: string | null
    sent_at: string | null
  }[]
  driver_calls: {
    status: string
    blocked_reason: string | null
    requested_by: string
    reason: string
    requested_at: string
  }[]
  audit: { action: string; actor: string; detail: unknown; occurred_at: string }[]
}

export interface AlertSummary {
  open_alerts: number
  by_severity: Partial<Record<Severity, number>>
  by_tms: Record<string, Partial<Record<Severity, number>>>
}

export interface MissionRow {
  tms_id: string
  mission_id?: string
  id?: string
  [key: string]: unknown
}

export interface MissionsResponse {
  count: number
  missions: MissionRow[]
  tms_time: Record<string, string>
  unreadable_tms: Record<string, string>
}

export interface MonitoringResponse {
  cycles: Record<string, unknown>[]
  investigations: Record<string, number>
  auth_disabled: boolean
}

export interface ReportItem {
  conversation_id: string
  produced_at: string
  report: string | null
}
