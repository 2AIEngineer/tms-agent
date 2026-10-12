import { cn } from '@/lib/utils'
import { SEVERITY_LABEL } from '@/lib/format'
import type { Severity } from '@/lib/types'

const TONE: Record<Severity, string> = {
  critical: 'bg-sev-critical/12 text-sev-critical ring-sev-critical/25',
  high: 'bg-sev-high/14 text-sev-high ring-sev-high/30',
  medium: 'bg-sev-medium/18 text-[color-mix(in_oklch,var(--sev-medium),var(--foreground)_45%)] ring-sev-medium/35',
  low: 'bg-sev-low/14 text-sev-low ring-sev-low/30',
}

const DOT: Record<Severity, string> = {
  critical: 'bg-sev-critical',
  high: 'bg-sev-high',
  medium: 'bg-sev-medium',
  low: 'bg-sev-low',
}

export function SeverityDot({ severity, className }: { severity: Severity; className?: string }) {
  return <span className={cn('inline-block size-2 shrink-0 rounded-full', DOT[severity], className)} />
}

export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-xs font-medium ring-1 ring-inset',
        TONE[severity],
        className,
      )}
    >
      <SeverityDot severity={severity} />
      {SEVERITY_LABEL[severity]}
    </span>
  )
}
