import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark' | 'system'
const KEY = 'tms-assistant.theme'

function read(): Theme {
  try {
    const v = localStorage.getItem(KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch {
    /* stockage indisponible */
  }
  return 'system'
}

function apply(theme: Theme) {
  const dark = theme === 'dark' || (theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
}

apply(read())

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(read)

  useEffect(() => {
    apply(theme)
    if (theme !== 'system') return
    const media = matchMedia('(prefers-color-scheme: dark)')
    const listener = () => apply('system')
    media.addEventListener('change', listener)
    return () => media.removeEventListener('change', listener)
  }, [theme])

  const set = useCallback((next: Theme) => {
    setTheme(next)
    try {
      localStorage.setItem(KEY, next)
    } catch {
      /* ignoré */
    }
  }, [])

  return { theme, setTheme: set }
}
