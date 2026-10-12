import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api, ApiError } from '@/lib/api'
import { postStream } from '@/lib/sse'
import type {
  AssistantAnswer,
  AssistantEvent,
  AssistantMode,
  ConversationMessage,
  PendingAction,
  ToolRole,
} from '@/lib/types'

export interface ToolStep {
  callId: string
  tool: string
  role: ToolRole
  args: Record<string, string>
  state: 'running' | 'ok' | 'error'
  durationMs?: number
  preview?: string
}

export interface Turn {
  id: string
  role: 'operator' | 'assistant'
  text: string
  reasoning: string
  steps: ToolStep[]
  round?: { round: number; max: number }
  streaming: boolean
  error?: string
  answer?: AssistantAnswer
  actionIds: string[]
  /** Mention affichée sous la réponse (ex. contexte de la question). */
  context?: { missionId?: string; alertId?: string }
}

export type ActionView = PendingAction & { busy?: 'confirm' | 'reject' }

export interface SendOptions {
  message: string
  mode: AssistantMode
  allowActions: boolean
  missionId?: string
  alertId?: string
}

let counter = 0
const uid = () => `t${Date.now().toString(36)}${(counter++).toString(36)}`

const blankAssistant = (): Turn => ({
  id: uid(),
  role: 'assistant',
  text: '',
  reasoning: '',
  steps: [],
  streaming: true,
  actionIds: [],
})

/** Reconstitue des tours à partir de l'historique : les lectures précèdent la réponse qui s'en sert. */
function turnsFromHistory(messages: ConversationMessage[]): Turn[] {
  const turns: Turn[] = []
  let steps: Turn['steps'] = []
  for (const m of messages) {
    if (m.role === 'operator') {
      turns.push({ id: uid(), role: 'operator', text: m.content, reasoning: '', steps: [], streaming: false, actionIds: [] })
    } else if (m.role === 'tool') {
      steps.push({
        callId: `${m.seq}`,
        tool: m.tool_name ?? 'outil',
        role: 'read',
        args: {},
        state: 'ok',
        preview: m.content.length > 400 ? `${m.content.slice(0, 400)}…` : m.content,
      })
    } else {
      turns.push({ id: uid(), role: 'assistant', text: m.content, reasoning: '', steps, streaming: false, actionIds: [] })
      steps = []
    }
  }
  return turns
}

export function useChat() {
  const queryClient = useQueryClient()
  const [turns, setTurns] = useState<Turn[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [actions, setActions] = useState<Record<string, ActionView>>({})
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const abort = useRef<AbortController | null>(null)
  const conversationRef = useRef<string | null>(null)

  useEffect(() => {
    conversationRef.current = conversationId
  }, [conversationId])

  const patchTurn = useCallback((id: string, patch: (t: Turn) => Turn) => {
    setTurns((all) => all.map((t) => (t.id === id ? patch(t) : t)))
  }, [])

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['pending'] })
    void queryClient.invalidateQueries({ queryKey: ['conversations'] })
    void queryClient.invalidateQueries({ queryKey: ['alerts'] })
  }, [queryClient])

  /** Applique un événement du flux au tour en cours. Retourne la réponse finale s'il y en a une. */
  const apply = useCallback(
    (turnId: string, event: AssistantEvent): AssistantAnswer | undefined => {
      switch (event.type) {
        case 'started':
          setConversationId(event.conversation_id)
          conversationRef.current = event.conversation_id
          break
        case 'round_started':
          patchTurn(turnId, (t) => ({ ...t, round: { round: event.round, max: event.max_rounds } }))
          break
        case 'reasoning_delta':
          patchTurn(turnId, (t) => ({ ...t, reasoning: t.reasoning + event.text }))
          break
        case 'tool_started':
          patchTurn(turnId, (t) => ({
            ...t,
            steps: [
              ...t.steps,
              { callId: event.call_id, tool: event.tool, role: event.role, args: event.arguments, state: 'running' },
            ],
          }))
          break
        case 'tool_completed':
          patchTurn(turnId, (t) => ({
            ...t,
            steps: t.steps.map((s) =>
              s.callId === event.call_id
                ? { ...s, state: event.ok ? 'ok' : 'error', durationMs: event.duration_ms, preview: event.preview }
                : s,
            ),
          }))
          break
        case 'text_delta':
          patchTurn(turnId, (t) => ({ ...t, text: t.text + event.text }))
          break
        case 'pending_action':
          setActions((a) => ({ ...a, [event.action.id]: event.action }))
          patchTurn(turnId, (t) => ({ ...t, actionIds: [...t.actionIds, event.action.id] }))
          void queryClient.invalidateQueries({ queryKey: ['pending'] })
          break
        case 'done':
          setConversationId(event.answer.conversation_id)
          patchTurn(turnId, (t) => ({
            ...t,
            // La réponse complète recale le texte diffusé : aucun compteur à tenir côté interface.
            text: event.answer.answer || t.text,
            streaming: false,
            round: undefined,
            answer: event.answer,
            actionIds: Array.from(new Set([...t.actionIds, ...event.answer.pending_actions.map((p) => p.id)])),
          }))
          setActions((a) => {
            const next = { ...a }
            for (const p of event.answer.pending_actions) next[p.id] = { ...p, ...next[p.id] }
            return next
          })
          return event.answer
        case 'error':
          patchTurn(turnId, (t) => ({ ...t, streaming: false, round: undefined, error: event.message }))
          break
      }
      return undefined
    },
    [patchTurn, queryClient],
  )

  const run = useCallback(
    async (turnId: string, open: (signal: AbortSignal) => ReturnType<typeof postStream>) => {
      const controller = new AbortController()
      abort.current = controller
      setBusy(true)
      let answer: AssistantAnswer | undefined
      try {
        for await (const message of await open(controller.signal)) {
          let event: AssistantEvent
          try {
            event = JSON.parse(message.data) as AssistantEvent
          } catch {
            continue
          }
          answer = apply(turnId, event) ?? answer
        }
        // Flux fermé sans événement final : ne pas laisser le curseur tourner.
        patchTurn(turnId, (t) => (t.streaming ? { ...t, streaming: false, round: undefined } : t))
      } catch (error) {
        const aborted = (error as Error).name === 'AbortError'
        patchTurn(turnId, (t) => ({
          ...t,
          streaming: false,
          round: undefined,
          error: aborted ? undefined : error instanceof ApiError ? error.message : 'Le flux a été interrompu.',
          text: aborted && !t.text ? '*Génération interrompue.*' : t.text,
        }))
      } finally {
        abort.current = null
        setBusy(false)
        refresh()
      }
      return answer
    },
    [apply, patchTurn, refresh],
  )

  const send = useCallback(
    async (options: SendOptions) => {
      const operator: Turn = {
        id: uid(),
        role: 'operator',
        text: options.message,
        reasoning: '',
        steps: [],
        streaming: false,
        actionIds: [],
        context: { missionId: options.missionId, alertId: options.alertId },
      }
      const assistant = blankAssistant()
      setTurns((all) => [...all, operator, assistant])
      await run(assistant.id, (signal) =>
        postStream(
          '/assistant/stream',
          {
            message: options.message,
            mode: options.mode,
            conversation_id: conversationRef.current,
            mission_id: options.missionId ?? null,
            alert_id: options.alertId ?? null,
            allow_actions: options.allowActions,
          },
          signal,
        ),
      )
    },
    [run],
  )

  const decide = useCallback(
    async (actionId: string, decision: 'confirm' | 'reject') => {
      setActions((a) => ({ ...a, [actionId]: { ...a[actionId], busy: decision } }))
      const assistant = blankAssistant()
      setTurns((all) => [...all, assistant])
      const answer = await run(assistant.id, (signal) =>
        postStream(`/assistant/actions/${actionId}/${decision}/stream`, undefined, signal),
      )
      setActions((a) => {
        const current = a[actionId]
        if (!current) return a
        if (!answer) return { ...a, [actionId]: { ...current, busy: undefined } }
        const failed = decision === 'confirm' && answer.actions.some((x) => !x.succeeded)
        return {
          ...a,
          [actionId]: {
            ...current,
            busy: undefined,
            status: decision === 'reject' ? 'rejected' : failed ? 'failed' : 'executed',
          },
        }
      })
      // Un tour vide (erreur de décision) n'a pas sa place dans le fil.
      setTurns((all) => all.filter((t) => t.id !== assistant.id || t.text || t.steps.length || t.error))
    },
    [run],
  )

  const stop = useCallback(() => abort.current?.abort(), [])

  const reset = useCallback(() => {
    abort.current?.abort()
    setTurns([])
    setConversationId(null)
    conversationRef.current = null
    setActions({})
  }, [])

  const load = useCallback(
    async (id: string) => {
      abort.current?.abort()
      setLoading(true)
      try {
        const [history, pending] = await Promise.all([api.conversation(id), api.pending().catch(() => null)])
        const loaded = turnsFromHistory(history.messages)
        const mine = (pending?.pending_actions ?? []).filter((p) => p.conversation_id === id && p.status === 'proposed')
        if (mine.length) {
          const last = [...loaded].reverse().find((t) => t.role === 'assistant')
          if (last) last.actionIds = mine.map((p) => p.id)
          else loaded.push({ ...blankAssistant(), streaming: false, text: 'Actions en attente de votre décision.', actionIds: mine.map((p) => p.id) })
        }
        setTurns(loaded)
        setActions(Object.fromEntries(mine.map((p) => [p.id, p])))
        setConversationId(id)
        conversationRef.current = id
      } catch (error) {
        toast.error(error instanceof ApiError ? error.message : 'Conversation illisible.')
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  /** Ajoute dans le fil une action en attente venue d'ailleurs (panneau « à valider »). */
  const adopt = useCallback((action: PendingAction) => {
    setActions((a) => ({ ...a, [action.id]: action }))
  }, [])

  useEffect(() => () => abort.current?.abort(), [])

  return { turns, conversationId, actions, busy, loading, send, decide, stop, reset, load, adopt }
}

export type Chat = ReturnType<typeof useChat>
