import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { getSettings, saveSettings } from '@/lib/api'

export function SettingsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient()
  const [baseUrl, setBaseUrl] = useState('')
  const [token, setToken] = useState('')

  useEffect(() => {
    if (open) {
      const s = getSettings()
      setBaseUrl(s.baseUrl)
      setToken(s.token)
    }
  }, [open])

  const save = () => {
    saveSettings({ baseUrl: baseUrl.trim() || '/api', token: token.trim() })
    void queryClient.invalidateQueries()
    toast.success('Réglages enregistrés.')
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Réglages de connexion</DialogTitle>
          <DialogDescription>
            Conservés dans ce navigateur uniquement. En développement, l’API tourne sans jeton
            (OPS_API_AUTH_DISABLED).
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">Adresse de l’API d’exploitation</span>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="/api ou http://127.0.0.1:8020" />
          </label>
          <label className="block space-y-1.5">
            <span className="text-sm font-medium">Jeton porteur (Entra ID)</span>
            <Input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Laisser vide sans authentification"
              autoComplete="off"
            />
          </label>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Annuler
          </Button>
          <Button onClick={save}>Enregistrer</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
