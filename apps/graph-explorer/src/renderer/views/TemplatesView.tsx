// The Templates view (docs/EXPLORER_TEMPLATES.md §4): author department
// archetypes and scenario playbooks as external plugins, via an agent running
// the repo's `author-playbook` skill, validated by the lint -> test-kit loop.
import { useEffect } from 'react'
import './templates.css'
import { useTemplatesStore } from '../store-templates.js'
import { TemplateLibrary } from './templates/Library.js'
import { AuthoringChat } from './templates/AuthoringChat.js'
import { FileViewer } from './templates/FileViewer.js'

export function TemplatesView(): JSX.Element {
  const loadList = useTemplatesStore((s) => s.loadList)
  const selectedSlug = useTemplatesStore((s) => s.selectedSlug)

  useEffect(() => {
    void loadList()
  }, [loadList])

  return (
    <div className="view view-templates templates-view">
      <aside className="templates-sidebar">
        <TemplateLibrary />
      </aside>
      <main className="templates-center">
        {selectedSlug ? (
          <AuthoringChat key={selectedSlug} slug={selectedSlug} />
        ) : (
          <div className="view-empty">
            <div className="view-empty-title">Templates</div>
            <div className="muted">Scaffold a new template, or pick one from the library, to start authoring.</div>
          </div>
        )}
      </main>
      <aside className="templates-rightbar">
        <FileViewer />
      </aside>
    </div>
  )
}
