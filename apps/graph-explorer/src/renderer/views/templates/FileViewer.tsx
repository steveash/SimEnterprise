// Right pane of the Templates view: a read-only view of a template's files
// (plugin.py / test_<slug>.py / template.json) with copy (docs/EXPLORER_TEMPLATES.md §4).
import { useState } from 'react'
import { useTemplatesStore } from '../../store-templates.js'

export function FileViewer(): JSX.Element {
  const fileViewer = useTemplatesStore((s) => s.fileViewer)
  const loadingFiles = useTemplatesStore((s) => s.loadingFiles)
  const [active, setActive] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  if (loadingFiles) return <div className="muted small tpl-fileviewer-empty">loading files…</div>
  if (!fileViewer) {
    return <div className="muted small tpl-fileviewer-empty">Select "View files" on a template to inspect it here.</div>
  }

  const file = fileViewer.files.find((f) => f.path === active) ?? fileViewer.files[0] ?? null

  const copy = () => {
    if (!file) return
    void navigator.clipboard?.writeText(file.content)
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <div className="tpl-fileviewer">
      <div className="section-title">
        Files · <span className="mono">{fileViewer.slug}</span>
      </div>
      <div className="tpl-file-tabs">
        {fileViewer.files.map((f) => (
          <button key={f.path} className={(active ?? fileViewer.files[0]?.path) === f.path ? 'active' : ''} onClick={() => setActive(f.path)}>
            {f.path}
          </button>
        ))}
      </div>
      {file && (
        <div className="artifact-view">
          <div className="artifact-head">
            <span className="muted small mono">{file.path}</span>
            <button className="copy-btn" onClick={copy}>
              {copied ? 'copied ✓' : 'copy'}
            </button>
          </div>
          <pre className="artifact-text mono">{file.content}</pre>
        </div>
      )}
    </div>
  )
}
