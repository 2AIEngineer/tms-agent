import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { cn } from '@/lib/utils'

export const Markdown = memo(function Markdown({
  children,
  streaming,
  className,
}: {
  children: string
  streaming?: boolean
  className?: string
}) {
  return (
    <div className={cn('prose-chat', streaming && 'stream-caret', className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{ a: (props) => <a {...props} target="_blank" rel="noreferrer noopener" /> }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
})
