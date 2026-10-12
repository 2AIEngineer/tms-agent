import { createContext, useContext } from 'react'
import type { Chat } from '@/hooks/use-chat'
import type { AssistantMode, Me } from '@/lib/types'

export type View = 'assistant' | 'alerts' | 'missions' | 'reports' | 'system'

/** Ce que l'opérateur rattache à sa question : une alerte, une mission. */
export interface AskContext {
  alertId?: string
  alertTitle?: string
  missionId?: string
  tmsId?: string
}

export interface Draft {
  text: string
  mode?: AssistantMode
  /** Incrémenté à chaque envoi pour re-déclencher la mise au point du champ. */
  nonce: number
}

export interface AppApi {
  chat: Chat
  me: Me | undefined
  view: View
  go: (view: View) => void
  context: AskContext
  setContext: (context: AskContext) => void
  draft: Draft | null
  setDraft: (draft: Draft | null) => void
  /** Ouvre l'assistant sur un sujet précis, éventuellement avec une question pré-remplie. */
  askAbout: (context: AskContext, text?: string) => void
  openConversation: (id: string) => void
  newConversation: () => void
  openSettings: () => void
  openPalette: () => void
}

export const AppContext = createContext<AppApi | null>(null)

export function useApp(): AppApi {
  const value = useContext(AppContext)
  if (!value) throw new Error('useApp hors du fournisseur')
  return value
}
