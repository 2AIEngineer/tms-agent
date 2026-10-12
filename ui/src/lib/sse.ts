import { ApiError, headers, url } from './api'

export interface SseMessage {
  event: string
  data: string
}

/** Découpe un flux SSE en messages. Les commentaires (`: keepalive`) sont ignorés. */
export async function* readSse(response: Response): AsyncGenerator<SseMessage> {
  if (!response.body) return
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += value
      let boundary: number
      while ((boundary = buffer.search(/\r?\n\r?\n/)) !== -1) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary).replace(/^\r?\n\r?\n/, '')
        let event = 'message'
        const data: string[] = []
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith(':')) continue
          if (line.startsWith('event:')) event = line.slice(6).trim()
          else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
        }
        if (data.length) yield { event, data: data.join('\n') }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/**
 * POST puis lecture du flux. `EventSource` ne sait faire que du GET, et la
 * question de l'opérateur voyage dans le corps.
 */
export async function postStream(
  path: string,
  body: unknown | undefined,
  signal: AbortSignal,
): Promise<AsyncGenerator<SseMessage>> {
  let response: Response
  try {
    response = await fetch(url(path), {
      method: 'POST',
      headers: { ...headers(body !== undefined), Accept: 'text/event-stream' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error
    throw new ApiError(0, "API injoignable. Vérifiez l'adresse dans les réglages.")
  }
  if (!response.ok) {
    let message = `Erreur ${response.status}`
    try {
      const b = await response.json()
      if (typeof b?.detail === 'string') message = b.detail
    } catch {
      /* corps non JSON */
    }
    throw new ApiError(response.status, message)
  }
  return readSse(response)
}

/** Flux GET persistant avec reprise automatique (alertes en direct). */
export function subscribe(
  path: string,
  onMessage: (message: SseMessage) => void,
  onState: (connected: boolean) => void,
): () => void {
  const controller = new AbortController()
  void (async () => {
    let delay = 1000
    while (!controller.signal.aborted) {
      try {
        const response = await fetch(url(path), {
          headers: { ...headers(), Accept: 'text/event-stream' },
          signal: controller.signal,
        })
        if (!response.ok) throw new Error(String(response.status))
        onState(true)
        delay = 1000
        for await (const message of readSse(response)) onMessage(message)
      } catch {
        /* coupure : on retente */
      }
      onState(false)
      if (controller.signal.aborted) return
      await new Promise((r) => setTimeout(r, delay))
      delay = Math.min(delay * 2, 15000)
    }
  })()
  return () => controller.abort()
}
